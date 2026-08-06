#!/usr/bin/env python3
"""Root-owned Medicam OTA activation and automatic rollback helper."""

from __future__ import annotations

import json
import os
import pwd
import re
import shutil
import base64
import hashlib
import secrets
import ssl
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


REPO = Path("/home/radxa/medicam-server")
STATE_DIR = Path("/var/lib/medicam-ota")
STATE_FILE = STATE_DIR / "status.json"
LOG_FILE = STATE_DIR / "update.log"
REQUEST_FILE = STATE_DIR / "request.json"
RELEASES_DIR = STATE_DIR / "releases"
MEDICAM_STATE_DIR = Path("/var/lib/medicam")
PROVISION_FILE = MEDICAM_STATE_DIR / "provision.json"
PROVISION_LOCK_FILE = MEDICAM_STATE_DIR / "provision.lock"
SYSTEM_SIGNERS = Path("/etc/medicam/ota_allowed_signers")
SYSTEM_IMAGE_VERSION = Path("/etc/medicam/image-version")
TRUSTED_COMMIT_FILE = Path("/etc/medicam/ota-current-commit")
HIGHEST_VERSION_FILE = Path("/etc/medicam/ota-highest-version")
INSTALLED_HELPER = Path("/usr/local/sbin/medicam-ota-activate")
SUDOERS_FILE = Path("/etc/sudoers.d/medicam")
DROP_IN = Path("/etc/systemd/system/medicam.service.d/runtime.conf")
PAIRING_SECRET_FILE = Path("/etc/medicam/pairing-secret")
TLS_DIR = Path("/etc/medicam/tls")
TLS_KEY_FILE = TLS_DIR / "key.pem"
TLS_CERT_FILE = TLS_DIR / "cert.pem"
BLE_ROOT = Path("/opt/medicam/ble")
BLE_VENV = Path("/opt/medicam/venv")
BLE_REQUIREMENTS_MARKER = Path("/opt/medicam/.requirements-sha256")
NFTABLES_FILE = Path("/etc/nftables.conf")
SSH_DROP_IN = Path("/etc/ssh/sshd_config.d/99-medicam.conf")
AVAHI_SERVICE_FILE = Path("/etc/avahi/services/medicam.service")
USB_POWER_RULES_FILE = Path("/etc/udev/rules.d/99-medicam-usb-power.rules")
USB_SYSFS_DIR = Path("/sys/bus/usb/devices")
USB_POWER_IDS = {
    ("eba4", "6579"),
    ("2109", "2817"),
    ("2109", "0817"),
}
SYSTEMD_ASSETS = {
    "medicam.service": Path("/etc/systemd/system/medicam.service"),
    "medicam-ble.service": Path("/etc/systemd/system/medicam-ble.service"),
    "medicam-ble-manager.service": Path(
        "/etc/systemd/system/medicam-ble-manager.service"
    ),
    "medicam-ota.path": Path("/etc/systemd/system/medicam-ota.path"),
    "medicam-ota-request.service": Path(
        "/etc/systemd/system/medicam-ota-request.service"
    ),
    "medicam-ble-refresh.path": Path(
        "/etc/systemd/system/medicam-ble-refresh.path"
    ),
    "medicam-ble-refresh.service": Path(
        "/etc/systemd/system/medicam-ble-refresh.service"
    ),
}
REQUIRED_SYSTEMD_ASSETS = {
    "medicam.service",
    "medicam-ble.service",
    "medicam-ble-manager.service",
}
TAG_PATTERN = re.compile(r"^medicam-v\d+\.\d+\.\d+$")
SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
HEALTH_TIMEOUT_SECONDS = 60
PERSISTENT_FILES = (
    ("camera_settings.json", 1024 * 1024),
    ("ffmpeg.log", 16 * 1024 * 1024),
)

SUDOERS_CONTENT = """# Managed by Medicam signed OTA. Do not edit manually.
radxa ALL=(root) NOPASSWD: /usr/local/sbin/medicam-ota-activate pairing-info
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


def run(
    command: list[str],
    *,
    timeout: float = 120,
    check: bool = True,
    env: dict[str, str] | None = None,
):
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    if check and result.returncode != 0:
        raise ActivationError(
            f"command failed ({result.returncode}): {' '.join(command)}: "
            f"{result.stderr.strip() or result.stdout.strip()}"
        )
    return result


def git(*arguments: str, check: bool = True):
    environment = os.environ.copy()
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            # Root verification must never refresh the unprivileged checkout's
            # cache index or change its owner.
            "GIT_OPTIONAL_LOCKS": "0",
            "HOME": "/var/empty",
        }
    )
    return run(
        [
            "/usr/bin/git",
            "-c",
            f"safe.directory={REPO}",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "gpg.ssh.program=/usr/bin/ssh-keygen",
            "-c",
            "core.sshCommand=/usr/bin/ssh",
            "-C",
            str(REPO),
            *arguments,
        ],
        check=check,
        env=environment,
    )


def _radxa_ids() -> tuple[int, int]:
    try:
        account = pwd.getpwnam("radxa")
        return account.pw_uid, account.pw_gid
    except KeyError:
        return os.getuid(), os.getgid()


def _chown_radxa(path: Path) -> None:
    try:
        uid, gid = _radxa_ids()
        os.chown(path, uid, gid)
    except (KeyError, PermissionError):
        pass


def _safe_atomic_write(
    path: Path,
    content: bytes,
    *,
    mode: int,
    owner: tuple[int, int] | None = None,
) -> None:
    """Atomically replace a file without following attacker-controlled names."""
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(path.parent, directory_flags)
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            mode,
            dir_fd=directory,
        )
        os.fchmod(descriptor, mode)
        if owner is not None:
            os.fchown(descriptor, *owner)
        with os.fdopen(descriptor, "wb", closefd=False) as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _safe_read_regular(path: Path, maximum_size: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > maximum_size:
            raise ActivationError(f"unsafe file: {path}")
        with os.fdopen(descriptor, "rb", closefd=False) as value:
            return value.read(maximum_size + 1)
    finally:
        os.close(descriptor)


def read_status() -> dict:
    try:
        value = json.loads(_safe_read_regular(STATE_FILE, 1024 * 1024))
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
    _safe_atomic_write(
        STATE_FILE,
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8"),
        mode=0o660,
        owner=_radxa_ids(),
    )
    append_log(
        f"state={state} progress={payload['progress']} message={message} "
        f"error={payload.get('error') or ''}"
    )
    return payload


def append_log(message: str) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        LOG_FILE,
        os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o660,
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ActivationError("OTA log is not a regular file")
        os.fchmod(descriptor, 0o660)
        os.fchown(descriptor, *_radxa_ids())
        os.write(descriptor, f"{utc_now()} {message}\n".encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


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
    dirty = git(
        "status",
        "--porcelain=v1",
        "--untracked-files=no",
    ).stdout.strip()
    if dirty:
        raise ActivationError("tracked working tree differs from signed target commit")


def materialize_release(target: str) -> Path:
    """Extract only bytes stored in the verified commit into a root-owned tree."""
    if not SHA_PATTERN.fullmatch(target):
        raise ActivationError("invalid release commit")
    RELEASES_DIR.mkdir(parents=True, exist_ok=True)
    # Release bytes are immutable/root-owned but world-readable so the
    # unprivileged backend installer can consume signed requirements without
    # ever granting it write access to the trusted release tree.
    os.chmod(RELEASES_DIR, 0o755)
    final = RELEASES_DIR / target
    if final.is_dir():
        marker = final / ".medicam-commit"
        try:
            if marker.read_text(encoding="ascii").strip() == target:
                os.chmod(final, 0o755)
                return final
        except OSError:
            pass
        shutil.rmtree(final)

    staging = Path(tempfile.mkdtemp(prefix=f".{target[:12]}-", dir=RELEASES_DIR))
    archive = staging / ".release.tar"
    try:
        git(
            "archive",
            "--format=tar",
            f"--output={archive}",
            target,
        )
        with tarfile.open(archive, mode="r:") as package:
            for member in package.getmembers():
                relative = Path(member.name)
                if (
                    member.name.startswith("/")
                    or ".." in relative.parts
                    or member.issym()
                    or member.islnk()
                    or member.isdev()
                ):
                    raise ActivationError(
                        f"unsafe path in signed release archive: {member.name}"
                    )
            package.extractall(staging)
        archive.unlink()
        _safe_atomic_write(
            staging / ".medicam-commit",
            f"{target}\n".encode("ascii"),
            mode=0o400,
        )
        os.chmod(staging, 0o755)
        os.replace(staging, final)
        return final
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


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
    _safe_atomic_write(path, content.encode("utf-8"), mode=mode)


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


def _device_id() -> str:
    source = ""
    for path in (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id")):
        try:
            source = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if source:
            break
    source = source or os.uname().nodename
    return hashlib.sha256(f"medicam:{source}".encode("utf-8")).hexdigest()[:8].upper()


def migrate_provision_state(uid: int, gid: int) -> None:
    """Move legacy credentials out of the checkout and remove the duplicate."""
    legacy_provision = REPO / "provision.json"
    if not PROVISION_FILE.exists():
        try:
            provision = _safe_read_regular(legacy_provision, 1024 * 1024)
            json.loads(provision)
        except (OSError, ValueError, json.JSONDecodeError, ActivationError):
            provision = b'{"provisioned":false,"info":{}}'
        _safe_atomic_write(
            PROVISION_FILE,
            provision,
            mode=0o600,
            owner=(uid, gid),
        )

    descriptor = os.open(
        PROVISION_FILE,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ActivationError("provision state is not a regular file")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as value:
            provision = json.load(value)
        if not isinstance(provision, dict):
            raise ActivationError("provision state is not a JSON object")
        os.fchmod(descriptor, 0o600)
        os.fchown(descriptor, uid, gid)
    except (ValueError, json.JSONDecodeError) as error:
        raise ActivationError("provision state is invalid JSON") from error
    finally:
        os.close(descriptor)

    # The production service reads only /var/lib/medicam/provision.json.
    # Keeping the migrated token in the writable Git checkout creates an
    # unnecessary second secret and can preserve legacy world-readable modes.
    legacy_provision.unlink(missing_ok=True)

    PROVISION_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    lock_descriptor = os.open(
        PROVISION_LOCK_FILE,
        os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
        0o660,
    )
    try:
        if not stat.S_ISREG(os.fstat(lock_descriptor).st_mode):
            raise ActivationError("provision lock is not a regular file")
        os.fchmod(lock_descriptor, 0o660)
        os.fchown(lock_descriptor, 0, gid)
    finally:
        os.close(lock_descriptor)


def ensure_security_identity() -> None:
    uid, gid = _radxa_ids()
    MEDICAM_STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(MEDICAM_STATE_DIR, 0, gid)
    os.chmod(MEDICAM_STATE_DIR, 0o770)

    migrate_provision_state(uid, gid)

    if not PAIRING_SECRET_FILE.exists():
        pairing_secret = base64.b32encode(secrets.token_bytes(16)).decode("ascii")
        pairing_secret = pairing_secret.rstrip("=")
        _safe_atomic_write(
            PAIRING_SECRET_FILE,
            f"{pairing_secret}\n".encode("ascii"),
            mode=0o600,
            owner=(0, 0),
        )
    else:
        secret = _safe_read_regular(PAIRING_SECRET_FILE, 256).decode("ascii").strip()
        if not re.fullmatch(r"[A-Z2-7]{26}", secret):
            raise ActivationError("invalid existing pairing secret")
        os.chmod(PAIRING_SECRET_FILE, 0o600, follow_symlinks=False)
        os.chown(PAIRING_SECRET_FILE, 0, 0, follow_symlinks=False)

    TLS_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(TLS_DIR, 0, gid)
    os.chmod(TLS_DIR, 0o750)
    if not TLS_KEY_FILE.exists() or not TLS_CERT_FILE.exists():
        with tempfile.TemporaryDirectory(dir=TLS_DIR) as directory:
            temporary_key = Path(directory) / "key.pem"
            temporary_cert = Path(directory) / "cert.pem"
            device_name = f"Medicam-{_device_id()[-6:]}"
            run(
                [
                    "/usr/bin/openssl",
                    "ecparam",
                    "-name",
                    "prime256v1",
                    "-genkey",
                    "-noout",
                    "-out",
                    str(temporary_key),
                ]
            )
            run(
                [
                    "/usr/bin/openssl",
                    "req",
                    "-new",
                    "-x509",
                    "-sha256",
                    "-days",
                    "3650",
                    "-key",
                    str(temporary_key),
                    "-out",
                    str(temporary_cert),
                    "-subj",
                    f"/CN={device_name}",
                    "-addext",
                    f"subjectAltName=DNS:nom.local,DNS:{device_name.lower()}.local",
                ]
            )
            _safe_atomic_write(
                TLS_KEY_FILE,
                temporary_key.read_bytes(),
                mode=0o640,
                owner=(0, gid),
            )
            _safe_atomic_write(
                TLS_CERT_FILE,
                temporary_cert.read_bytes(),
                mode=0o644,
                owner=(0, 0),
            )
    else:
        os.chmod(TLS_KEY_FILE, 0o640, follow_symlinks=False)
        os.chown(TLS_KEY_FILE, 0, gid, follow_symlinks=False)
        os.chmod(TLS_CERT_FILE, 0o644, follow_symlinks=False)
        os.chown(TLS_CERT_FILE, 0, 0, follow_symlinks=False)


def install_ble_runtime(release_root: Path) -> None:
    source_app = release_root / "app"
    requirements = release_root / "requirements.txt"
    ble_unit = release_root / "deploy" / "systemd" / "medicam-ble.service"
    try:
        uses_root_owned_runtime = "/opt/medicam/" in ble_unit.read_text(
            encoding="utf-8"
        )
    except OSError:
        uses_root_owned_runtime = False
    if not uses_root_owned_runtime:
        # Releases before priority 7 execute BLE from the unprivileged checkout;
        # do not build an unused root runtime while rolling back to them.
        return
    if not source_app.is_dir() or not requirements.is_file():
        raise ActivationError("signed BLE runtime source is missing")

    BLE_ROOT.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".ble-", dir=BLE_ROOT.parent))
    try:
        shutil.copytree(source_app, staging / "app")
        if BLE_ROOT.exists():
            shutil.rmtree(BLE_ROOT)
        os.replace(staging, BLE_ROOT)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    requirement_bytes = requirements.read_bytes()
    requirement_hash = hashlib.sha256(requirement_bytes).hexdigest()
    installed_hash = ""
    try:
        installed_hash = BLE_REQUIREMENTS_MARKER.read_text(encoding="ascii").strip()
    except OSError:
        pass
    if not (BLE_VENV / "bin" / "python3").is_file():
        if BLE_VENV.exists():
            shutil.rmtree(BLE_VENV)
        run(["/usr/bin/python3", "-m", "venv", str(BLE_VENV)], timeout=180)
        installed_hash = ""
    if installed_hash != requirement_hash:
        run(
            [
                str(BLE_VENV / "bin" / "pip"),
                "install",
                "--disable-pip-version-check",
                "--no-input",
                "-r",
                str(requirements),
            ],
            timeout=600,
        )
        _safe_atomic_write(
            BLE_REQUIREMENTS_MARKER,
            f"{requirement_hash}\n".encode("ascii"),
            mode=0o444,
            owner=(0, 0),
        )


def install_network_security(release_root: Path) -> None:
    nft_source = release_root / "deploy" / "nftables" / "medicam.nft"
    ssh_source = release_root / "deploy" / "ssh" / "medicam.conf"
    avahi_source = release_root / "deploy" / "avahi" / "medicam.service"
    if (
        not nft_source.is_file()
        or not ssh_source.is_file()
        or not avahi_source.is_file()
    ):
        # Backward-compatible rollback to releases predating priority 7 keeps
        # the already-installed policy instead of deleting it.
        return

    if (
        not Path("/usr/sbin/nft").is_file()
        or not Path("/usr/sbin/avahi-daemon").is_file()
    ):
        environment = os.environ.copy()
        environment["DEBIAN_FRONTEND"] = "noninteractive"
        run(["/usr/bin/apt-get", "update"], timeout=600, env=environment)
        run(
            [
                "/usr/bin/apt-get",
                "install",
                "-y",
                "--no-install-recommends",
                "nftables",
                "avahi-daemon",
            ],
            timeout=600,
            env=environment,
        )
    run(["/usr/sbin/nft", "-c", "-f", str(nft_source)])
    AVAHI_SERVICE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _safe_atomic_write(
        NFTABLES_FILE,
        nft_source.read_bytes(),
        mode=0o644,
        owner=(0, 0),
    )
    run(["/usr/sbin/nft", "-f", str(NFTABLES_FILE)])
    run(["/bin/systemctl", "enable", "--now", "nftables.service"])

    previous_ssh = None
    try:
        previous_ssh = _safe_read_regular(SSH_DROP_IN, 1024 * 1024)
    except (OSError, ActivationError):
        pass
    _safe_atomic_write(
        SSH_DROP_IN,
        ssh_source.read_bytes(),
        mode=0o644,
        owner=(0, 0),
    )
    validation = run(["/usr/sbin/sshd", "-t"], check=False)
    if validation.returncode != 0:
        if previous_ssh is None:
            SSH_DROP_IN.unlink(missing_ok=True)
        else:
            _safe_atomic_write(
                SSH_DROP_IN,
                previous_ssh,
                mode=0o644,
                owner=(0, 0),
            )
        raise ActivationError(f"refusing invalid SSH policy: {validation.stderr}")
    run(["/bin/systemctl", "reload", "ssh.service"], check=False)

    _safe_atomic_write(
        AVAHI_SERVICE_FILE,
        avahi_source.read_bytes(),
        mode=0o644,
        owner=(0, 0),
    )
    run(["/bin/systemctl", "enable", "--now", "avahi-daemon.service"])
    run(["/bin/systemctl", "restart", "avahi-daemon.service"])

    for unit in (
        "cups.service",
        "cups-browsed.service",
        "dnsmasq.service",
        "smbd.service",
        "nmbd.service",
        "samba-ad-dc.service",
    ):
        run(["/bin/systemctl", "disable", "--now", unit], check=False)


def install_usb_power_policy(release_root: Path) -> None:
    source = release_root / "deploy" / "udev" / "99-medicam-usb-power.rules"
    if not source.is_file():
        # Rollbacks to releases before diagnostics keep the harmless installed
        # protection rather than re-enabling autosuspend mid-capture.
        return
    _safe_atomic_write(
        USB_POWER_RULES_FILE,
        source.read_bytes(),
        mode=0o644,
        owner=(0, 0),
    )
    run(["/usr/bin/udevadm", "control", "--reload-rules"])

    # udev applies the rule on the next add event. Protect devices that are
    # already attached as part of this OTA without logically unplugging them.
    for device in USB_SYSFS_DIR.glob("*"):
        try:
            identity = (
                (device / "idVendor").read_text(encoding="ascii").strip().lower(),
                (device / "idProduct").read_text(encoding="ascii").strip().lower(),
            )
            if identity in USB_POWER_IDS:
                (device / "power" / "control").write_text("on\n", encoding="ascii")
        except OSError:
            continue


def normalize_git_runtime_ownership() -> None:
    """Keep backend-owned Git cache files writable after root verification."""
    uid, gid = _radxa_ids()
    for path in (REPO / ".git" / "index", REPO / ".git" / "ORIG_HEAD"):
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
        except FileNotFoundError:
            continue
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ActivationError(f"unsafe Git runtime file: {path}")
            os.fchown(descriptor, uid, gid)
        finally:
            os.close(descriptor)


def install_release_assets(release_root: Path, *, harden: bool = True) -> None:
    source_helper = release_root / "scripts" / "medicam_ota_activate.py"
    source_signers = release_root / "deploy" / "ota_allowed_signers"
    source_image = release_root / "deploy" / "image-version"
    for source in (source_helper, source_signers, source_image):
        if not source.is_file():
            raise ActivationError(f"signed release asset missing: {source}")
    for unit_name in SYSTEMD_ASSETS:
        source = release_root / "deploy" / "systemd" / unit_name
        if unit_name in REQUIRED_SYSTEMD_ASSETS and not source.is_file():
            raise ActivationError(f"signed systemd unit missing: {source}")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    os.chown(STATE_DIR, 0, _radxa_ids()[1])
    os.chmod(STATE_DIR, 0o770)
    ensure_security_identity()
    install_ble_runtime(release_root)
    install_network_security(release_root)
    install_usb_power_policy(release_root)
    SYSTEM_SIGNERS.parent.mkdir(parents=True, exist_ok=True)
    INSTALLED_HELPER.parent.mkdir(parents=True, exist_ok=True)
    DROP_IN.parent.mkdir(parents=True, exist_ok=True)
    _safe_atomic_write(
        SYSTEM_SIGNERS,
        source_signers.read_bytes(),
        mode=0o644,
        owner=(0, 0),
    )
    _safe_atomic_write(
        SYSTEM_IMAGE_VERSION,
        source_image.read_bytes(),
        mode=0o644,
        owner=(0, 0),
    )
    _atomic_install_text(DROP_IN, RUNTIME_DROP_IN, 0o644)
    for unit_name, destination in SYSTEMD_ASSETS.items():
        source = release_root / "deploy" / "systemd" / unit_name
        if not source.is_file():
            # Rollback to a release predating the root path activator keeps
            # the installed idle path unit but restores every required unit.
            continue
        _atomic_install_text(destination, source.read_text(encoding="utf-8"), 0o644)

    # Install the next signed helper atomically. The currently running Python
    # process remains mapped, while the scheduled process uses this new file.
    _safe_atomic_write(
        INSTALLED_HELPER,
        source_helper.read_bytes(),
        mode=0o755,
        owner=(0, 0),
    )
    normalize_git_runtime_ownership()
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
    run(["/bin/systemctl", "enable", "--now", "medicam-ota.path"])
    run(["/bin/systemctl", "enable", "--now", "medicam-ble-refresh.path"])


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


def install_python_requirements(release_root: Path) -> None:
    pip = REPO / "medicam-venv" / "bin" / "pip"
    requirements = release_root / "requirements.txt"
    if not requirements.is_file():
        raise ActivationError("signed requirements file is missing")
    run(
        [
            "/usr/sbin/runuser",
            "-u",
            "radxa",
            "--",
            str(pip),
            "install",
            "--disable-pip-version-check",
            "--no-input",
            "-r",
            str(requirements),
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
        _safe_atomic_write(
            path,
            content,
            mode=mode,
            owner=(uid, gid),
        )


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
            urls = [("http://127.0.0.1:8000/ping", None)]
            if TLS_CERT_FILE.is_file():
                context = ssl.create_default_context(cafile=str(TLS_CERT_FILE))
                context.check_hostname = False
                urls.insert(0, ("https://127.0.0.1:8000/ping", context))
            last_connection_error = None
            payload = None
            for url, context in urls:
                try:
                    with urllib.request.urlopen(
                        url,
                        timeout=2,
                        context=context,
                    ) as response:
                        payload = json.load(response)
                    break
                except Exception as error:
                    last_connection_error = error
            if payload is None:
                raise last_connection_error or ActivationError("backend did not answer")
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
        target_release = materialize_release(target)
        write_status(
            "installing",
            83,
            "Preparing signed security assets",
            previous_commit=previous,
            target_commit=target,
            target_tag=tag,
        )
        # Repeat dependency installation from root-owned, signed release bytes.
        # This is idempotent and makes activation independent of the lifetime
        # of the old sandboxed backend process.
        install_python_requirements(target_release)
        # The schedule command may still be running an older helper that does
        # not know how to create TLS identity or the root-owned BLE runtime.
        # Reinstalling from the verified materialized commit here makes the
        # migration safe and idempotent across helper generations.
        install_release_assets(target_release, harden=True)
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
        previous_release = materialize_release(previous)
        install_python_requirements(previous_release)
        install_release_assets(previous_release, harden=True)
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
    target_release = materialize_release(target)
    persistent_files = snapshot_persistent_files()
    try:
        install_release_assets(target_release, harden=True)
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
            previous_release = materialize_release(previous)
            install_python_requirements(previous_release)
            install_release_assets(previous_release, harden=True)
        except Exception as restore_error:
            raise ActivationError(
                f"activation scheduling failed: {activation_error}; "
                f"previous release restoration also failed: {restore_error}"
            ) from restore_error
        raise ActivationError(
            f"activation scheduling failed; previous release restored: "
            f"{activation_error}"
        ) from activation_error


def _read_activation_request() -> dict:
    descriptor = os.open(
        REQUEST_FILE,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        metadata = os.fstat(descriptor)
        expected_uid = _radxa_ids()[0]
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != expected_uid
            or metadata.st_mode & 0o077
            or metadata.st_size > 16 * 1024
        ):
            raise ActivationError("unsafe OTA activation request")
        with os.fdopen(descriptor, "rb", closefd=False) as source:
            raw = source.read(16 * 1024 + 1)
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ActivationError("invalid OTA activation request JSON") from error
    if not isinstance(payload, dict) or payload.get("format") != 1:
        raise ActivationError("unsupported OTA activation request")
    allowed = {
        "format",
        "job_id",
        "previous_commit",
        "target_commit",
        "target_tag",
        "requested_at",
    }
    if set(payload) - allowed:
        raise ActivationError("unexpected OTA activation request fields")
    if not re.fullmatch(r"[0-9a-f]{32}", str(payload.get("job_id") or "")):
        raise ActivationError("invalid OTA activation job id")
    if not SHA_PATTERN.fullmatch(str(payload.get("previous_commit") or "")):
        raise ActivationError("invalid OTA previous commit")
    if not SHA_PATTERN.fullmatch(str(payload.get("target_commit") or "")):
        raise ActivationError("invalid OTA target commit")
    if not TAG_PATTERN.fullmatch(str(payload.get("target_tag") or "")):
        raise ActivationError("invalid OTA target tag")
    return payload


def consume_activation_request() -> None:
    try:
        request = _read_activation_request()
        REQUEST_FILE.unlink(missing_ok=True)
        append_log(
            f"consuming activation request job={request['job_id']} "
            f"tag={request['target_tag']}"
        )
        schedule(
            request["previous_commit"],
            request["target_commit"],
            request["target_tag"],
        )
    except Exception as error:
        REQUEST_FILE.unlink(missing_ok=True)
        append_log(f"activation request failed: {error}")
        write_status(
            "failed",
            100,
            "OTA activation request failed",
            error_code="activation_schedule_failed",
            error=str(error),
        )
        if isinstance(error, ActivationError):
            raise
        raise ActivationError(str(error)) from error


def main(arguments: list[str]) -> int:
    if len(arguments) == 2 and arguments[1] == "harden":
        # Establish root-owned trust before removing the temporary migration
        # privilege. A malformed or unsigned checkout therefore cannot lock the
        # administrator out of retrying the one-time transition.
        commit, tag = initialize_trust_state()
        release_root = materialize_release(commit)
        install_release_assets(release_root, harden=False)
        harden_sudoers()
        print(f"Medicam OTA hardened at {tag} ({commit})")
        return 0
    if len(arguments) == 2 and arguments[1] == "pairing-info":
        ensure_security_identity()
        secret = _safe_read_regular(PAIRING_SECRET_FILE, 256).decode("ascii").strip()
        grouped = "-".join(secret[index:index + 4] for index in range(0, len(secret), 4))
        print(f"Device ID: {_device_id()}")
        print(f"Pairing code: {grouped}")
        print(f"QR payload: medicam://pair?device_id={_device_id()}&code={secret}")
        return 0
    if len(arguments) == 2 and arguments[1] == "consume-request":
        consume_activation_request()
        print("Medicam OTA activation request consumed")
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
        "{harden|pairing-info|consume-request|schedule PREVIOUS TARGET TAG|"
        "perform PREVIOUS TARGET TAG}",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv))
    except ActivationError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
