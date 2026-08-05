"""Signed-release OTA discovery, preparation, and progress state."""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path


REPO_DIR = Path(
    os.environ.get(
        "MEDICAM_REPO_DIR",
        Path(__file__).resolve().parents[1],
    )
).resolve()
TAG_PATTERN = re.compile(r"^medicam-v(\d+)\.(\d+)\.(\d+)$")
ALLOWED_SIGNERS_FILE = Path(
    os.environ.get(
        "MEDICAM_OTA_ALLOWED_SIGNERS",
        REPO_DIR / "deploy" / "ota_allowed_signers",
    )
)
STATE_DIR = Path(os.environ.get("MEDICAM_OTA_STATE_DIR", "/var/lib/medicam-ota"))
STATE_FILE = STATE_DIR / "status.json"
UPDATE_LOG_FILE = STATE_DIR / "update.log"
ACTIVATOR = os.environ.get(
    "MEDICAM_OTA_ACTIVATOR",
    "/usr/local/sbin/medicam-ota-activate",
)
ACTIVE_STATES = {
    "queued",
    "checking",
    "downloading",
    "verifying",
    "installing",
    "restarting",
    "healthchecking",
    "rolling_back",
}
PERSISTENT_FILES = (
    ("camera_settings.json", 1024 * 1024),
    ("provision.json", 1024 * 1024),
    ("ffmpeg.log", 16 * 1024 * 1024),
)

_UPDATE_LOCK = threading.RLock()


class UpdateError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class UpdateBusyError(UpdateError):
    pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run(
    cmd: list[str],
    *,
    timeout: float = 60,
    cwd: Path | None = REPO_DIR,
) -> dict:
    try:
        process = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return {
            "ok": process.returncode == 0,
            "stdout": process.stdout.strip(),
            "stderr": process.stderr.strip(),
            "returncode": process.returncode,
        }
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(error),
            "returncode": -1,
        }


def _git(*args: str, timeout: float = 60) -> dict:
    return _run(["git", *args], timeout=timeout)


def get_local_commit() -> str | None:
    result = _git("rev-parse", "HEAD")
    return result["stdout"] if result["ok"] else None


def _parse_tag(tag: str) -> tuple[int, int, int] | None:
    match = TAG_PATTERN.fullmatch(tag)
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _tag_commit(tag: str) -> str | None:
    result = _git("rev-list", "-n", "1", tag)
    commit = result["stdout"].strip()
    return commit if result["ok"] and re.fullmatch(r"[0-9a-f]{40}", commit) else None


def _verify_tag(tag: str) -> tuple[bool, str]:
    if _parse_tag(tag) is None:
        return False, "invalid_release_tag"
    if not ALLOWED_SIGNERS_FILE.is_file():
        return False, "ota_allowed_signers_missing"
    result = _git(
        "-c",
        "gpg.format=ssh",
        "-c",
        f"gpg.ssh.allowedSignersFile={ALLOWED_SIGNERS_FILE}",
        "verify-tag",
        "--raw",
        tag,
    )
    if result["ok"]:
        return True, ""
    reason = result["stderr"] or result["stdout"] or "invalid_release_signature"
    return False, reason[-1000:]


def _remote_release_tags() -> dict[str, str]:
    remote = _git(
        "ls-remote",
        "--tags",
        "--refs",
        "origin",
        "refs/tags/medicam-v*",
        timeout=90,
    )
    if not remote["ok"]:
        raise UpdateError(
            "release_channel_unavailable",
            remote["stderr"] or "Could not query signed release tags",
        )

    tags: dict[str, str] = {}
    for line in remote["stdout"].splitlines():
        sha, separator, ref = line.partition("\t")
        tag = ref.removeprefix("refs/tags/")
        if separator and re.fullmatch(r"[0-9a-f]{40}", sha) and _parse_tag(tag):
            tags[tag] = sha
    return tags


def _fetch_release_tags() -> None:
    result = _git(
        "fetch",
        "--prune",
        "--force",
        "origin",
        "+refs/tags/medicam-v*:refs/tags/medicam-v*",
        timeout=180,
    )
    if not result["ok"]:
        raise UpdateError(
            "release_download_failed",
            result["stderr"] or "Could not download release tags",
        )


def _is_ancestor(ancestor: str, descendant: str) -> bool:
    return _git("merge-base", "--is-ancestor", ancestor, descendant)["ok"]


def _current_signed_release(local_commit: str | None) -> dict | None:
    if not local_commit:
        return None
    result = _git("tag", "--points-at", local_commit, "--list", "medicam-v*")
    if not result["ok"]:
        return None
    verified = []
    for tag in result["stdout"].splitlines():
        version = _parse_tag(tag.strip())
        valid, _ = _verify_tag(tag.strip()) if version else (False, "")
        if version and valid:
            verified.append((version, tag.strip()))
    if not verified:
        return None
    version, tag = max(verified)
    return {
        "tag": tag,
        "version": ".".join(str(part) for part in version),
        "commit": local_commit,
        "signature_verified": True,
    }


def check_for_update() -> dict:
    local_commit = get_local_commit()
    current_release = _current_signed_release(local_commit)
    try:
        remote_tags = _remote_release_tags()
        _fetch_release_tags()
    except UpdateError as error:
        return {
            "ok": False,
            "channel": "signed-stable",
            "local": local_commit,
            "remote": None,
            "current_release": current_release,
            "latest_release": None,
            "update_available": False,
            "error_code": error.code,
            "error": error.message,
        }

    trusted = []
    untrusted = []
    for tag in remote_tags:
        version = _parse_tag(tag)
        valid, reason = _verify_tag(tag)
        commit = _tag_commit(tag) if valid else None
        if version and valid and commit:
            trusted.append((version, tag, commit))
        else:
            untrusted.append({"tag": tag, "reason": reason})

    latest = None
    if trusted:
        version, tag, commit = max(trusted)
        latest = {
            "tag": tag,
            "version": ".".join(str(part) for part in version),
            "commit": commit,
            "signature_verified": True,
        }

    update_available = False
    if latest and local_commit and latest["commit"] != local_commit:
        current_version = (
            _parse_tag(str(current_release.get("tag")))
            if current_release
            else None
        )
        latest_version = _parse_tag(str(latest["tag"]))
        if current_version is not None and latest_version is not None:
            # SemVer is authoritative after the device enters the signed
            # channel. This prevents a lower but unrelated signed commit from
            # being presented as an update.
            update_available = latest_version > current_version
        else:
            # One-time migration from an untagged installation: allow a signed
            # release unless it is already an ancestor of the local checkout.
            update_available = not _is_ancestor(latest["commit"], local_commit)
    return {
        "ok": True,
        "channel": "signed-stable",
        # Legacy fields remain for one app release during protocol migration.
        "local": local_commit,
        "remote": latest["commit"] if latest else None,
        "current_release": current_release,
        "latest_release": latest,
        "update_available": update_available,
        "untrusted_tags": untrusted,
    }


def _atomic_write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _append_log(message: str) -> None:
    try:
        UPDATE_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(UPDATE_LOG_FILE, "a", encoding="utf-8") as output:
            output.write(f"{_utc_now()} {message}\n")
    except OSError:
        pass


def _set_status(state: str, progress: int, **fields) -> dict:
    with _UPDATE_LOCK:
        previous = get_update_status()
        payload = {
            "job_id": fields.pop("job_id", previous.get("job_id")),
            "state": state,
            "progress": max(0, min(100, int(progress))),
            "message": fields.pop("message", state),
            "updated_at": _utc_now(),
            "started_at": previous.get("started_at") or _utc_now(),
            **{
                key: value
                for key, value in previous.items()
                if key
                not in {
                    "job_id",
                    "state",
                    "progress",
                    "message",
                    "updated_at",
                    "started_at",
                }
            },
            **fields,
        }
        _atomic_write_json(STATE_FILE, payload)
        _append_log(
            f"job={payload.get('job_id')} state={state} "
            f"progress={payload['progress']} message={payload['message']}"
        )
        return payload


def get_update_status() -> dict:
    try:
        payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("state"), str):
            return payload
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return {
        "job_id": None,
        "state": "idle",
        "progress": 0,
        "message": "Ready to check for updates",
        "updated_at": None,
        "started_at": None,
    }


def _snapshot_persistent_files() -> dict[str, tuple[bytes, int]]:
    snapshot = {}
    for relative_path, maximum_size in PERSISTENT_FILES:
        path = REPO_DIR / relative_path
        try:
            if path.is_symlink() or not path.is_file():
                continue
            metadata = path.stat()
            if metadata.st_size > maximum_size:
                _append_log(
                    f"persistent file too large to snapshot: {relative_path}"
                )
                continue
            snapshot[relative_path] = (
                path.read_bytes(),
                metadata.st_mode & 0o777,
            )
        except OSError as error:
            _append_log(f"persistent snapshot failed for {relative_path}: {error}")
    return snapshot


def _restore_persistent_files(snapshot: dict[str, tuple[bytes, int]]) -> None:
    for relative_path, (content, mode) in snapshot.items():
        path = REPO_DIR / relative_path
        temporary = path.with_name(f".{path.name}.ota-tmp")
        try:
            with open(temporary, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def _restore_previous_checkout(
    previous_commit: str,
    persistent_files: dict[str, tuple[bytes, int]],
) -> None:
    _git("reset", "--hard", previous_commit, timeout=60)
    _restore_persistent_files(persistent_files)
    pip = REPO_DIR / "medicam-venv" / "bin" / "pip"
    if pip.is_file():
        _run(
            [str(pip), "install", "-r", "requirements.txt"],
            timeout=300,
        )


def run_update(job_id: str) -> dict:
    previous_commit = get_local_commit()
    persistent_files = _snapshot_persistent_files()
    target_commit = None
    target_tag = None
    try:
        _set_status(
            "checking",
            10,
            job_id=job_id,
            message="Checking signed release channel",
            previous_commit=previous_commit,
        )
        release_info = check_for_update()
        if not release_info.get("ok"):
            raise UpdateError(
                str(release_info.get("error_code") or "update_check_failed"),
                str(release_info.get("error") or "Update check failed"),
            )
        latest = release_info.get("latest_release")
        if not release_info.get("update_available") or not latest:
            return _set_status(
                "no_update",
                100,
                job_id=job_id,
                message="The latest signed release is already installed",
                current_commit=previous_commit,
                release=release_info.get("current_release") or latest,
            )

        target_commit = str(latest["commit"])
        target_tag = str(latest["tag"])
        _set_status(
            "downloading",
            30,
            job_id=job_id,
            message=f"Downloaded {target_tag}",
            target_commit=target_commit,
            target_tag=target_tag,
        )

        valid, reason = _verify_tag(target_tag)
        if not valid or _tag_commit(target_tag) != target_commit:
            raise UpdateError(
                "release_signature_invalid",
                reason or "Signed tag does not match the target commit",
            )
        _set_status(
            "verifying",
            45,
            job_id=job_id,
            message="Release signature and commit hash verified",
            signature_verified=True,
        )

        checkout = _git("reset", "--hard", target_commit, timeout=60)
        if not checkout["ok"]:
            raise UpdateError(
                "release_checkout_failed",
                checkout["stderr"] or "Could not activate release checkout",
            )
        _restore_persistent_files(persistent_files)
        _set_status(
            "installing",
            65,
            job_id=job_id,
            message="Installing release dependencies",
        )
        pip = REPO_DIR / "medicam-venv" / "bin" / "pip"
        install = _run(
            [str(pip), "install", "-r", "requirements.txt"],
            timeout=300,
        )
        if not install["ok"]:
            raise UpdateError(
                "dependency_install_failed",
                install["stderr"] or "Could not install release dependencies",
            )

        _set_status(
            "restarting",
            80,
            job_id=job_id,
            message="Camera is restarting into the signed release",
        )
        schedule = _run(
            [
                "sudo",
                "-n",
                ACTIVATOR,
                "schedule",
                str(previous_commit),
                target_commit,
                target_tag,
            ],
            timeout=30,
            cwd=None,
        )
        if not schedule["ok"]:
            raise UpdateError(
                "activation_schedule_failed",
                schedule["stderr"] or "Could not schedule release activation",
            )
        return get_update_status()
    except UpdateError as error:
        if previous_commit and target_commit and get_local_commit() != previous_commit:
            _restore_previous_checkout(previous_commit, persistent_files)
        return _set_status(
            "failed",
            100,
            job_id=job_id,
            message="Update preparation failed",
            error_code=error.code,
            error=error.message,
            previous_commit=previous_commit,
            target_commit=target_commit,
            target_tag=target_tag,
        )
    except Exception as error:
        if previous_commit and target_commit and get_local_commit() != previous_commit:
            _restore_previous_checkout(previous_commit, persistent_files)
        return _set_status(
            "failed",
            100,
            job_id=job_id,
            message="Unexpected update failure",
            error_code="unexpected_update_failure",
            error=str(error),
            previous_commit=previous_commit,
            target_commit=target_commit,
            target_tag=target_tag,
        )


def _update_worker(job_id: str) -> None:
    run_update(job_id)


def start_update() -> dict:
    with _UPDATE_LOCK:
        current = get_update_status()
        if current.get("state") in ACTIVE_STATES:
            raise UpdateBusyError("update_in_progress", "An update is already running")
        job_id = uuid.uuid4().hex
        status = _set_status(
            "queued",
            5,
            job_id=job_id,
            message="Update queued",
            started_at=_utc_now(),
            error=None,
            error_code=None,
            previous_commit=get_local_commit(),
            target_commit=None,
            target_tag=None,
            failed_commit=None,
            failed_tag=None,
            rollback=False,
        )
        thread = threading.Thread(
            target=_update_worker,
            args=(job_id,),
            name=f"medicam-ota-{job_id[:8]}",
            daemon=True,
        )
        thread.start()
        return status
