#!/usr/bin/env python3
"""Root-owned Medicam OTA activation and automatic rollback helper."""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


REPO = Path("/home/radxa/medicam-server")
STATE_DIR = Path("/var/lib/medicam-ota")
STATE_FILE = STATE_DIR / "status.json"
LOG_FILE = STATE_DIR / "update.log"
SYSTEM_SIGNERS = Path("/etc/medicam/ota_allowed_signers")
SYSTEM_IMAGE_VERSION = Path("/etc/medicam/image-version")
TRUSTED_COMMIT_FILE = Path("/etc/medicam/ota-current-commit")
HIGHEST_VERSION_FILE = Path("/etc/medicam/ota-highest-version")
INSTALLED_HELPER = Path("/usr/local/sbin/medicam-ota-activate")
SUDOERS_FILE = Path("/etc/sudoers.d/medicam")
DROP_IN = Path("/etc/systemd/system/medicam.service.d/runtime.conf")
SYSTEMD_ASSETS = {
    "medicam.service": Path("/etc/systemd/system/medicam.service"),
    "medicam-ble.service": Path("/etc/systemd/system/medicam-ble.service"),
    "medicam-ble-manager.service": Path(
        "/etc/systemd/system/medicam-ble-manager.service"
    ),
}
TAG_PATTERN = re.compile(r"^medicam-v\d+\.\d+\.\d+$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
HEALTH_TIMEOUT_SECONDS = 60
PERSISTENT_FILES = (
    ("camera_settings.json", 1024 * 1024),
    ("provision.json", 1024 * 1024),
    ("ffmpeg.log", 16 * 1024 * 1024),
)

SUDOERS_CONTENT = """# Managed by Medicam signed OTA. Do not edit manually.
Cmnd_Alias MEDICAM_BLE = /bin/systemctl start medicam-ble.service, /bin/systemctl restart medicam-ble.service
Cmnd_Alias MEDICAM_OTA = /usr/local/sbin/medicam-ota-activate schedule *
radxa ALL=(root) NOPASSWD: MEDICAM_BLE, MEDICAM_OTA
"""

RUNTIME_DROP_IN = """[Service]
RuntimeDirectory=medicam
RuntimeDirectoryMode=0750
RuntimeDirectoryPreserve=restart
"""

LEGACY_SUDOERS_PATTERNS = (
    re.compile(r"^\s*radxa\s+ALL=.*NOPASSWD:\s*ALL\s*$"),
    re.compile(r"^\s*radxa\s+ALL=NOPASSWD:\s*/usr/bin/systemd-run\s*$"),
    re.compile(r"^\s*radxa\s+ALL=NOPASSWD:\s*/bin/bash\s+/home/radxa/medicam-server/restart_service\.sh\s*$"),
    re.compile(r"^\s*radxa\s+ALL=NOPASSWD:\s*/bin/systemctl\s+restart\s+medicam\.service\s*$"),
    re.compile(r"^\s*radxa\s+ALL=NOPASSWD:\s*/bin/systemctl\s+start\s+restart-medicam\.service\s*$"),
)


class ActivationError(RuntimeError):
    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def run(command: list[str], *, timeout: float = 120, check: bool = True):
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
    )
    if check and result.returncode != 0:
        raise ActivationError(
            f"command failed ({result.returncode}): {' '.join(command)}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def git(*arguments: str, check: bool = True):
    return run(
        [
            "git",
            "-c",
            f"safe.directory={REPO}",
            "-C",
            str(REPO),
            *arguments,
        ],
        check=check,
    )


def _radxa_ids() -> tuple[int, int]:
    account = pwd.getpwnam("radxa")
    return account.pw_uid, account.pw_gid


def _chown_radxa(path: Path) -> None:
    try:
        uid, gid = _radxa_ids()
        os.chown(path, uid, gid)
    except (KeyError, PermissionError):
        pass


def read_status() -> dict:
    try:
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def write_status(state: str, progress: int, message: str, **fields) -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    previous = read_status()
    payload = {
        **previous,
        "state": state,
        "progress": max(0, min(100, int(progress))),
        "message": message,
        "updated_at": utc_now(),
        **fields,
    }
    temporary = STATE_FILE.with_name(f"{STATE_FILE.name}.tmp")
    with open(temporary, "w", encoding="utf-8") as output:
        json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, STATE_FILE)
    os.chmod(STATE_FILE, 0o664)
    _chown_radxa(STATE_FILE)
    append_log(
        f"state={state} progress={payload['progress']} message={message} "
        f"error={payload.get('error') or ''}"
    )
    return payload


def append_log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as output:
        output.write(f"{utc_now()} {message}\n")
    os.chmod(LOG_FILE, 0o664)
    _chown_radxa(LOG_FILE)


def verify_release(tag: str, target: str) -> None:
    if not TAG_PATTERN.fullmatch(tag) or not SHA_PATTERN.fullmatch(target):
        raise ActivationError("invalid release tag or commit hash")
    if not SYSTEM_SIGNERS.is_file():
        raise ActivationError("system OTA allowed-signers file is missing")
    verified = git(
        "-c",
        "gpg.format=ssh",
        "-c",
        f"gpg.ssh.allowedSignersFile={SYSTEM_SIGNERS}",
        "verify-tag",
        "--raw",
        tag,
        check=False,
    )
    if verified.returncode != 0:
        raise ActivationError(
            "release signature verification failed: "
            + (verified.stderr.strip() or verified.stdout.strip())[-1000:]
        )
    resolved = git("rev-list", "-n", "1", tag).stdout.strip()
    if resolved != target:
        raise ActivationError("signed tag does not resolve to target commit")
    head = git("rev-parse", "HEAD").stdout.strip()
    if head != target:
        raise ActivationError("working tree does not match signed target commit")


def tag_version(tag: str) -> tuple[int, int, int]:
    if not TAG_PATTERN.fullmatch(tag):
        raise ActivationError("invalid stable release tag")
    return tuple(int(part) for part in tag.removeprefix("medicam-v").split("."))


def _read_trust_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError as error:
        raise ActivationError(f"OTA trust state is missing: {path}") from error


def write_trust_state(commit: str, tag: str) -> None:
    if not SHA_PATTERN.fullmatch(commit):
        raise ActivationError("refusing invalid trusted commit")
    version = tag_version(tag)
    SYSTEM_SIGNERS.parent.mkdir(parents=True, exist_ok=True)
    _atomic_install_text(TRUSTED_COMMIT_FILE, f"{commit}\n", 0o644)
    _atomic_install_text(
        HIGHEST_VERSION_FILE,
        ".".join(str(part) for part in version) + "\n",
        0o644,
    )


def validate_transition(previous: str, target_tag: str) -> None:
    trusted_previous = _read_trust_file(TRUSTED_COMMIT_FILE)
    if previous != trusted_previous:
        raise ActivationError("previous commit does not match root-owned trust state")
    highest_text = _read_trust_file(HIGHEST_VERSION_FILE)
    try:
        highest = tuple(int(part) for part in highest_text.split("."))
    except ValueError as error:
        raise ActivationError("invalid root-owned highest OTA version") from error
    if len(highest) != 3 or tag_version(target_tag) < highest:
        raise ActivationError("signed release downgrade is not allowed")


def initialize_trust_state() -> tuple[str, str]:
    head = git("rev-parse", "HEAD").stdout.strip()
    tags = git("tag", "--points-at", head, "--list", "medicam-v*").stdout.splitlines()
    verified = []
    for tag in tags:
        try:
            verify_release(tag.strip(), head)
            verified.append((tag_version(tag.strip()), tag.strip()))
        except ActivationError:
            continue
    if not verified:
        raise ActivationError("current checkout is not a signed stable release")
    _, tag = max(verified)
    write_trust_state(head, tag)
    return head, tag


def _atomic_install_text(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, mode)
    os.replace(temporary, path)


def _remove_legacy_sudoers_rules() -> None:
    candidates = [Path("/etc/sudoers")]
    directory = Path("/etc/sudoers.d")
    if directory.is_dir():
        candidates.extend(
            path
            for path in directory.iterdir()
            if path.is_file() and path != SUDOERS_FILE
        )

    for path in candidates:
        try:
            original = path.read_text(encoding="utf-8")
        except OSError:
            continue
        lines = original.splitlines(keepends=True)
        filtered = [
            line
            for line in lines
            if not any(pattern.fullmatch(line.strip()) for pattern in LEGACY_SUDOERS_PATTERNS)
        ]
        if filtered == lines:
            continue
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as output:
            output.writelines(filtered)
            temporary = Path(output.name)
        os.chmod(temporary, 0o440)
        validation = run(["/usr/sbin/visudo", "-cf", str(temporary)], check=False)
        if validation.returncode != 0:
            temporary.unlink(missing_ok=True)
            raise ActivationError(
                f"refusing invalid sudoers rewrite for {path}: {validation.stderr}"
            )
        os.replace(temporary, path)


def harden_sudoers() -> None:
    SUDOERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=SUDOERS_FILE.parent,
        delete=False,
    ) as output:
        output.write(SUDOERS_CONTENT)
        temporary = Path(output.name)
    os.chmod(temporary, 0o440)
    validation = run(["/usr/sbin/visudo", "-cf", str(temporary)], check=False)
    if validation.returncode != 0:
        temporary.unlink(missing_ok=True)
        raise ActivationError(f"invalid Medicam sudoers policy: {validation.stderr}")
    os.replace(temporary, SUDOERS_FILE)
    _remove_legacy_sudoers_rules()
    validation = run(["/usr/sbin/visudo", "-c"], check=False)
    if validation.returncode != 0:
        raise ActivationError(f"system sudoers validation failed: {validation.stderr}")


def install_release_assets(*, harden: bool = True) -> None:
    source_helper = REPO / "scripts" / "medicam_ota_activate.py"
    source_signers = REPO / "deploy" / "ota_allowed_signers"
    source_image = REPO / "deploy" / "image-version"
    for source in (source_helper, source_signers, source_image):
        if not source.is_file():
            raise ActivationError(f"signed release asset missing: {source}")
    for unit_name in SYSTEMD_ASSETS:
        source = REPO / "deploy" / "systemd" / unit_name
        if not source.is_file():
            raise ActivationError(f"signed systemd unit missing: {source}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(STATE_DIR, 0o775)
    _chown_radxa(STATE_DIR)
    SYSTEM_SIGNERS.parent.mkdir(parents=True, exist_ok=True)
    INSTALLED_HELPER.parent.mkdir(parents=True, exist_ok=True)
    DROP_IN.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_signers, SYSTEM_SIGNERS)
    os.chmod(SYSTEM_SIGNERS, 0o644)
    shutil.copyfile(source_image, SYSTEM_IMAGE_VERSION)
    os.chmod(SYSTEM_IMAGE_VERSION, 0o644)
    _atomic_install_text(DROP_IN, RUNTIME_DROP_IN, 0o644)
    for unit_name, destination in SYSTEMD_ASSETS.items():
        source = REPO / "deploy" / "systemd" / unit_name
        _atomic_install_text(destination, source.read_text(encoding="utf-8"), 0o644)

    # Install the next signed helper atomically. The currently running Python
    # process remains mapped, while the scheduled process uses this new file.
    temporary_helper = INSTALLED_HELPER.with_name(f".{INSTALLED_HELPER.name}.tmp")
    shutil.copyfile(source_helper, temporary_helper)
    os.chmod(temporary_helper, 0o755)
    os.replace(temporary_helper, INSTALLED_HELPER)
    if harden:
        harden_sudoers()
    run(["/bin/systemctl", "daemon-reload"])
    run(
        [
            "/bin/systemctl",
            "enable",
            "medicam.service",
            "medicam-ble.service",
            "medicam-ble-manager.service",
        ]
    )


def reset_checkout(commit: str) -> None:
    if not SHA_PATTERN.fullmatch(commit):
        raise ActivationError("invalid rollback commit")
    run(
        [
            "/usr/sbin/runuser",
            "-u",
            "radxa",
            "--",
            "git",
            "-C",
            str(REPO),
            "reset",
            "--hard",
            commit,
        ]
    )


def install_python_requirements() -> None:
    pip = REPO / "medicam-venv" / "bin" / "pip"
    run(
        [
            "/usr/sbin/runuser",
            "-u",
            "radxa",
            "--",
            str(pip),
            "install",
            "-r",
            str(REPO / "requirements.txt"),
        ],
        timeout=300,
    )


def snapshot_persistent_files() -> dict[str, tuple[bytes, int, int, int]]:
    snapshot = {}
    for relative_path, maximum_size in PERSISTENT_FILES:
        path = REPO / relative_path
        try:
            if path.is_symlink() or not path.is_file():
                continue
            metadata = path.stat()
            if metadata.st_size > maximum_size:
                append_log(
                    f"persistent file too large to snapshot: {relative_path}"
                )
                continue
            snapshot[relative_path] = (
                path.read_bytes(),
                metadata.st_mode & 0o777,
                metadata.st_uid,
                metadata.st_gid,
            )
        except OSError as error:
            append_log(f"persistent snapshot failed for {relative_path}: {error}")
    return snapshot


def restore_persistent_files(
    snapshot: dict[str, tuple[bytes, int, int, int]],
) -> None:
    for relative_path, (content, mode, uid, gid) in snapshot.items():
        path = REPO / relative_path
        temporary = path.with_name(f".{path.name}.ota-root-tmp")
        try:
            with open(temporary, "wb") as output:
                output.write(content)
                output.flush()
                os.fsync(output.fileno())
            os.chmod(temporary, mode)
            os.chown(temporary, uid, gid)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


def restart_services() -> None:
    run(
        [
            "/bin/systemctl",
            "restart",
            "medicam.service",
            "medicam-ble-manager.service",
        ],
        timeout=60,
    )
    run(
        ["/bin/systemctl", "try-restart", "medicam-ble.service"],
        timeout=30,
        check=False,
    )


def wait_for_health(expected_commit: str, timeout: int = HEALTH_TIMEOUT_SECONDS) -> tuple[bool, str]:
    deadline = time.monotonic() + timeout
    last_error = "backend did not answer"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:8000/ping",
                timeout=2,
            ) as response:
                payload = json.load(response)
            if payload.get("status") != "ok":
                last_error = f"unexpected ping payload: {payload}"
            elif payload.get("commit") != expected_commit:
                last_error = (
                    f"backend commit {payload.get('commit')} does not match "
                    f"expected {expected_commit}"
                )
            else:
                return True, ""
        except Exception as error:
            last_error = str(error)
        time.sleep(1)
    return False, last_error


def collect_failure_reason(health_error: str) -> str:
    journal = run(
        [
            "/bin/journalctl",
            "-u",
            "medicam.service",
            "-n",
            "40",
            "--no-pager",
        ],
        timeout=15,
        check=False,
    )
    tail = (journal.stdout or journal.stderr).strip()[-6000:]
    return f"{health_error}\n{tail}".strip()


def perform(previous: str, target: str, tag: str) -> None:
    persistent_files = snapshot_persistent_files()
    try:
        verify_release(tag, target)
        validate_transition(previous, tag)
        write_status(
            "restarting",
            85,
            "Restarting camera services",
            previous_commit=previous,
            target_commit=target,
            target_tag=tag,
        )
        try:
            restart_services()
            write_status(
                "healthchecking",
                92,
                "Checking updated backend health",
            )
            healthy, health_error = wait_for_health(target)
        except Exception as error:
            healthy = False
            health_error = f"service restart failed: {error}"
        if healthy:
            write_trust_state(target, tag)
            write_status(
                "complete",
                100,
                f"Signed release {tag} installed",
                current_commit=target,
                release={
                    "tag": tag,
                    "version": tag.removeprefix("medicam-v"),
                    "commit": target,
                    "signature_verified": True,
                },
                rollback=False,
                error=None,
                error_code=None,
            )
            return

        try:
            reason = collect_failure_reason(health_error)
        except Exception as error:
            reason = f"{health_error}; failed to read service log: {error}"
        write_status(
            "rolling_back",
            95,
            "Healthcheck failed; restoring previous release",
            error_code="updated_backend_unhealthy",
            error=reason,
            rollback=True,
        )
        reset_checkout(previous)
        restore_persistent_files(persistent_files)
        install_python_requirements()
        install_release_assets(harden=True)
        restart_services()
        restored, rollback_error = wait_for_health(previous)
        if not restored:
            raise ActivationError(f"rollback healthcheck failed: {rollback_error}")
        write_status(
            "rolled_back",
            100,
            "Update failed and the previous release was restored",
            current_commit=previous,
            failed_commit=target,
            failed_tag=tag,
            rollback=True,
            error_code="updated_backend_unhealthy",
            error=reason,
        )
    except Exception as error:
        append_log(f"activation failure: {error}")
        write_status(
            "rollback_failed",
            100,
            "Automatic rollback failed; service intervention is required",
            rollback=True,
            error_code="rollback_failed",
            error=str(error),
            previous_commit=previous,
            target_commit=target,
            target_tag=tag,
        )
        raise


def schedule(previous: str, target: str, tag: str) -> None:
    if not SHA_PATTERN.fullmatch(previous):
        raise ActivationError("invalid previous commit")
    verify_release(tag, target)
    validate_transition(previous, tag)
    persistent_files = snapshot_persistent_files()
    try:
        install_release_assets(harden=True)
        write_status(
            "restarting",
            82,
            "Activation scheduled; camera will restart",
            previous_commit=previous,
            target_commit=target,
            target_tag=tag,
            signature_verified=True,
        )
        unit = f"medicam-ota-{target[:12]}-{int(time.time())}"
        run(
            [
                "/usr/bin/systemd-run",
                "--unit",
                unit,
                "--on-active=1",
                "--collect",
                str(INSTALLED_HELPER),
                "perform",
                previous,
                target,
                tag,
            ]
        )
    except Exception as activation_error:
        # Asset installation happens before the service restart so systemd can
        # load the new signed units. If that preparation is interrupted, put
        # both the checkout and every privileged asset back before returning
        # the failure to the still-running backend.
        try:
            reset_checkout(previous)
            restore_persistent_files(persistent_files)
            install_python_requirements()
            install_release_assets(harden=True)
        except Exception as restore_error:
            raise ActivationError(
                f"activation scheduling failed: {activation_error}; "
                f"previous release restoration also failed: {restore_error}"
            ) from restore_error
        raise ActivationError(
            f"activation scheduling failed; previous release restored: "
            f"{activation_error}"
        ) from activation_error


def main(arguments: list[str]) -> int:
    if len(arguments) == 2 and arguments[1] == "harden":
        # Establish root-owned trust before removing the temporary migration
        # privilege. A malformed or unsigned checkout therefore cannot lock the
        # administrator out of retrying the one-time transition.
        install_release_assets(harden=False)
        commit, tag = initialize_trust_state()
        harden_sudoers()
        print(f"Medicam OTA hardened at {tag} ({commit})")
        return 0
    if len(arguments) == 5 and arguments[1] == "schedule":
        schedule(arguments[2], arguments[3], arguments[4])
        print("Medicam OTA activation scheduled")
        return 0
    if len(arguments) == 5 and arguments[1] == "perform":
        perform(arguments[2], arguments[3], arguments[4])
        return 0
    print(
        "usage: medicam-ota-activate "
        "{harden|schedule PREVIOUS TARGET TAG|perform PREVIOUS TARGET TAG}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except ActivationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
