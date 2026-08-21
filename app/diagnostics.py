"""Product health, privacy-safe support bundles, and hardware self-tests."""

from __future__ import annotations

import copy
import fcntl
import hashlib
import io
import json
import os
import platform
import re
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from app import audio, camera, preview, storage_manager, updater, utils, version_info


SELF_TEST_FILE = Path(
    os.environ.get("MEDICAM_SELF_TEST_FILE", "/var/lib/medicam/last-self-test.json")
)
MAX_LOG_BYTES = 512 * 1024
MAX_JOURNAL_LINES = 500
SELF_TEST_CAPTURE_FRAMES = 30
SELF_TEST_CAPTURE_TIMEOUT_SECONDS = 12
SERVICE_UNITS = (
    "medicam.service",
    "medicam-ble-manager.service",
    "medicam-ble.service",
    "avahi-daemon.service",
    "nftables.service",
)
REQUIRED_SERVICE_UNITS = {
    "medicam.service",
    "medicam-ble-manager.service",
    "avahi-daemon.service",
    "nftables.service",
}
LOG_FILES = {
    "ffmpeg.log": lambda: Path(camera.FFMPEG_LOG_FILE),
    "update.log": lambda: updater.UPDATE_LOG_FILE,
}

_HARDWARE_OPERATION_LOCK = threading.Lock()
_HARDWARE_OPERATION_DESCRIPTOR: int | None = None
_SELF_TEST_ACTIVE = threading.Event()
_SENSITIVE_VALUE_RE = re.compile(
    r"(?i)(api[_-]?token|x-medicam-token|authorization|password|passwd|psk|secret)"
    r"([\"']?\s*[=:]\s*[\"']?)([^\s,;\"']+)"
)
_AUTH_HEADER_RE = re.compile(
    r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s,;]+"
)
_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----",
    re.DOTALL,
)
HARDWARE_OPERATION_LOCK_FILE = Path(
    os.environ.get(
        "MEDICAM_HARDWARE_OPERATION_LOCK_FILE",
        (
            "/var/lib/medicam/hardware-operation.lock"
            if platform.system() == "Linux"
            else str(Path(tempfile.gettempdir()) / "medicam-hardware-operation.lock")
        ),
    )
)
_USB_ERROR_RE = re.compile(
    r"(?i)(no such device|vidioc_dqbuf|input/output error|usb disconnect|"
    r"device disconnected|cannot open video device)"
)


class SelfTestBusyError(RuntimeError):
    """Raised when another diagnostic test already owns the hardware."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _command(command: list[str], timeout: float = 5) -> dict:
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "ok": False,
            "returncode": -1,
            "stdout": "",
            "stderr": str(error),
        }


def _read_text(path: Path, maximum: int = 4096) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > maximum:
            return None
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


def _service_status(unit: str) -> dict:
    result = _command(
        [
            "/bin/systemctl",
            "show",
            unit,
            "--property=ActiveState,SubState,UnitFileState",
            "--value",
        ]
    )
    values = result["stdout"].splitlines() if result["ok"] else []
    active = values[0] if len(values) > 0 else "unavailable"
    sub = values[1] if len(values) > 1 else "unknown"
    enabled = values[2] if len(values) > 2 else "unknown"
    return {
        "ok": active == "active",
        "active": active,
        "sub": sub,
        "enabled": enabled,
    }


def _thermal_status() -> list[dict]:
    result = []
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        raw_temp = _read_text(zone / "temp")
        if raw_temp is None:
            continue
        try:
            temperature = float(raw_temp)
            if abs(temperature) > 1000:
                temperature /= 1000
        except ValueError:
            continue
        result.append(
            {
                "zone": zone.name,
                "type": _read_text(zone / "type") or "unknown",
                "celsius": round(temperature, 1),
            }
        )
    return result


def _memory_status() -> dict:
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            key, separator, raw = line.partition(":")
            if separator and key in {"MemTotal", "MemAvailable"}:
                values[key] = int(raw.strip().split()[0]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return {
        "total_bytes": values.get("MemTotal", 0),
        "available_bytes": values.get("MemAvailable", 0),
    }


def _usb_device_entry(path: Path) -> dict | None:
    vendor_id = _read_text(path / "idVendor")
    product_id = _read_text(path / "idProduct")
    if not vendor_id or not product_id:
        return None
    return {
        "sysfs": path.name,
        "vendor_id": vendor_id,
        "product_id": product_id,
        "manufacturer": _read_text(path / "manufacturer"),
        "product": _read_text(path / "product"),
        "speed_mbps": _read_text(path / "speed"),
        "bus_number": _read_text(path / "busnum"),
        "device_number": _read_text(path / "devnum"),
        "maximum_power": _read_text(path / "bMaxPower"),
        "power_control": _read_text(path / "power" / "control"),
        "autosuspend_seconds": _read_text(path / "power" / "autosuspend"),
        "runtime_status": _read_text(path / "power" / "runtime_status"),
        "authorized": _read_text(path / "authorized"),
    }


def get_usb_topology(camera_device: str | None = None) -> dict:
    device = camera_device or camera.find_camera_device(timeout=0.0)
    if not device:
        return {"camera_device": None, "chain": [], "hub_detected": False}
    node = Path(os.path.realpath(device)).name
    try:
        current = (Path("/sys/class/video4linux") / node / "device").resolve()
    except OSError:
        current = Path()
    chain = []
    visited = set()
    for candidate in (current, *current.parents):
        if candidate in visited:
            continue
        visited.add(candidate)
        entry = _usb_device_entry(candidate)
        if entry:
            chain.append(entry)
    return {
        "camera_device": device,
        "chain": chain,
        "hub_detected": len(chain) > 1,
        "camera_speed_mbps": chain[0].get("speed_mbps") if chain else None,
        # Linux Foundation root hubs are kernel-managed and are not targeted
        # by our product-specific udev policy.
        "power_protected": bool(chain)
        and all(
            item.get("power_control") == "on"
            for item in chain
            if item.get("vendor_id", "").lower() != "1d6b"
        ),
    }


def _tail_file(path: Path, maximum: int = MAX_LOG_BYTES) -> str:
    try:
        if path.is_symlink() or not path.is_file():
            return ""
        with path.open("rb") as source:
            size = source.seek(0, os.SEEK_END)
            source.seek(max(0, size - maximum))
            value = source.read(maximum)
        return value.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _usb_error_summary() -> dict:
    lines = [
        line.strip()
        for line in _tail_file(Path(camera.FFMPEG_LOG_FILE)).splitlines()
        if _USB_ERROR_RE.search(line)
    ]
    return {
        "detected_count": len(lines),
        "recent": lines[-5:],
    }


def _wifi_health() -> dict:
    connected = utils.is_wifi_connected()
    return {
        "connected": connected,
        "ssid": utils.get_wifi_ssid() if connected else "",
        "ip": utils.get_primary_ipv4() if connected else "",
    }


def get_health() -> dict:
    recording = camera.get_recording_status()
    settings = camera.get_settings()
    devices = audio.list_capture_devices()
    selected_audio = audio.resolve_capture_device(settings.get("audio_device", "auto"))
    storage = storage_manager.get_storage_info()
    wifi = _wifi_health()
    services = {unit: _service_status(unit) for unit in SERVICE_UNITS}
    usb = get_usb_topology(recording.get("camera", {}).get("device"))
    issues = []

    def issue(code: str, severity: str, message: str) -> None:
        issues.append({"code": code, "severity": severity, "message": message})

    if not wifi["connected"]:
        issue("wifi_disconnected", "error", "Wi-Fi is not connected")
    if not recording.get("camera", {}).get("available"):
        issue("camera_unavailable", "error", "USB camera is not available")
    if settings.get("audio_enabled") and selected_audio is None:
        issue("microphone_unavailable", "error", "Configured microphone is unavailable")
    if storage.get("critical_space"):
        issue("storage_critical", "error", "Storage is below the recording reserve")
    elif storage.get("low_space"):
        issue("storage_low", "warning", "Storage is running low")
    if recording.get("interrupted"):
        issue("recording_interrupted", "error", "The last recording was interrupted")
    for unit in REQUIRED_SERVICE_UNITS:
        if not services[unit]["ok"]:
            issue("service_unavailable", "error", f"{unit} is not active")
    if usb["chain"] and not usb["power_protected"]:
        issue("usb_autosuspend_enabled", "warning", "USB autosuspend protection is not active")
    usb_errors = _usb_error_summary()
    if usb_errors["detected_count"]:
        issue("usb_disconnect_history", "warning", "USB errors exist in the recorder log")

    severity = "healthy"
    if any(item["severity"] == "error" for item in issues):
        severity = "error"
    elif issues:
        severity = "warning"
    return {
        "status": severity,
        "checked_at": _utc_now(),
        "device": {
            "id": utils.get_device_id(),
            "name": utils.get_device_name(),
            "hostname": socket.gethostname(),
        },
        "wifi": wifi,
        "storage": storage,
        "camera": recording.get("camera", {}),
        "microphone": {
            "enabled": bool(settings.get("audio_enabled")),
            "found": selected_audio is not None,
            "selected": selected_audio,
            "device_count": len(devices),
        },
        "recording": recording,
        "services": services,
        "ble": services.get("medicam-ble.service", {}),
        "version": version_info.get_version_info(),
        "update": updater.get_update_status(),
        "usb": {**usb, "log_errors": usb_errors},
        "system": {
            "platform": platform.platform(),
            "kernel": platform.release(),
            "load_average": list(os.getloadavg()) if hasattr(os, "getloadavg") else [],
            "memory": _memory_status(),
            "thermal": _thermal_status(),
        },
        "self_test": get_last_self_test(),
        "issues": issues,
    }


def is_self_test_running() -> bool:
    return _SELF_TEST_ACTIVE.is_set()


def _begin_hardware_operation() -> bool:
    global _HARDWARE_OPERATION_DESCRIPTOR

    if not _HARDWARE_OPERATION_LOCK.acquire(blocking=False):
        return False
    descriptor = None
    try:
        HARDWARE_OPERATION_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(HARDWARE_OPERATION_LOCK_FILE, flags, 0o600)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("hardware_operation_lock_not_regular")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _HARDWARE_OPERATION_DESCRIPTOR = descriptor
        return True
    except (BlockingIOError, OSError):
        if descriptor is not None:
            os.close(descriptor)
        _HARDWARE_OPERATION_LOCK.release()
        return False


def _end_hardware_operation() -> None:
    global _HARDWARE_OPERATION_DESCRIPTOR

    descriptor = _HARDWARE_OPERATION_DESCRIPTOR
    _HARDWARE_OPERATION_DESCRIPTOR = None
    if descriptor is not None:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
    if _HARDWARE_OPERATION_LOCK.locked():
        _HARDWARE_OPERATION_LOCK.release()


def begin_recording_start() -> bool:
    """Reserve UVC/ALSA while the recorder opens both devices."""
    if _SELF_TEST_ACTIVE.is_set():
        return False
    return _begin_hardware_operation()


def end_recording_start() -> None:
    _end_hardware_operation()


def _test_result(name: str, outcome: str, started: float, **fields) -> dict:
    return {
        "name": name,
        **fields,
        # Nested probes may expose their own transport-level status="ok".
        # The normalized self-test outcome must remain passed/warning/failed.
        "status": outcome,
        "duration_ms": round((time.monotonic() - started) * 1000),
    }


def _camera_capture_test() -> dict:
    started = time.monotonic()
    device = camera.find_camera_device(timeout=2.0)
    if not device:
        return _test_result(
            "camera", "failed", started, code="camera_unavailable", frames=0
        )
    settings = camera.get_settings()
    resolution = camera.SUPPORTED_RESOLUTIONS.get(
        settings.get("resolution", "FHD"), "1920x1080"
    )
    runtime_dir = Path(camera.AUDIO_TEMP_DIR)
    if not runtime_dir.is_dir() or not os.access(runtime_dir, os.W_OK):
        runtime_dir = Path(tempfile.gettempdir())
    output = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix="medicam-self-test-", suffix=".mjpeg", dir=runtime_dir, delete=False
        ) as temporary:
            output = Path(temporary.name)
        command = [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-thread_queue_size", camera.LINUX_INPUT_QUEUE_SIZE,
            "-f", "v4l2", "-input_format", "mjpeg", "-framerate", "30",
            "-video_size", resolution, "-i", device,
            "-map", "0:v:0", "-frames:v", str(SELF_TEST_CAPTURE_FRAMES),
            "-c:v", "copy", "-an", "-f", "mjpeg", str(output),
        ]
        result = _command(command, timeout=SELF_TEST_CAPTURE_TIMEOUT_SECONDS)
        frames = camera.count_mjpeg_frames(str(output)) if output.exists() else 0
        passed = result["ok"] and frames >= SELF_TEST_CAPTURE_FRAMES
        return _test_result(
            "camera",
            "passed" if passed else "failed",
            started,
            code=None if passed else "camera_capture_failed",
            device=device,
            resolution=resolution,
            requested_frames=SELF_TEST_CAPTURE_FRAMES,
            frames=frames,
            details=result["stderr"][-1000:] if not passed else "",
        )
    finally:
        if output is not None:
            output.unlink(missing_ok=True)


def _audio_test() -> dict:
    started = time.monotonic()
    settings = camera.get_settings()
    configured = settings.get("audio_device", "auto")
    try:
        measured = audio.measure_audio_level(configured, 1)
        status = "passed" if measured.get("signal_detected") else "warning"
        return _test_result("audio", status, started, **measured)
    except audio.AudioError as error:
        return _test_result(
            "audio", "failed", started, code=error.code, details=error.details
        )


def _storage_write_test() -> dict:
    started = time.monotonic()
    path = None
    payload = b"medicam-storage-self-test\n" * 4096
    expected = hashlib.sha256(payload).hexdigest()
    try:
        Path(utils.VIDEOS_DIR).mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix=".medicam-self-test-", dir=utils.VIDEOS_DIR, delete=False
        ) as output:
            path = Path(output.name)
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        passed = actual == expected
        return _test_result(
            "storage",
            "passed" if passed else "failed",
            started,
            code=None if passed else "storage_verify_failed",
            bytes_written=len(payload),
        )
    except OSError as error:
        return _test_result(
            "storage", "failed", started, code="storage_write_failed", details=str(error)
        )
    finally:
        if path is not None:
            path.unlink(missing_ok=True)


def _network_test() -> dict:
    started = time.monotonic()
    wifi = _wifi_health()
    if not wifi["connected"] or not wifi["ip"]:
        return _test_result(
            "network", "failed", started, code="wifi_disconnected", **wifi
        )
    ip_command = shutil.which("ip") or "/usr/bin/ip"
    route = _command([ip_command, "-4", "route", "show", "default"])
    match = re.search(r"\bvia\s+(\d+(?:\.\d+){3})\b", route["stdout"])
    gateway = match.group(1) if match else None
    reachable = None
    if gateway:
        ping = shutil.which("ping")
        if ping:
            reachable = _command([ping, "-c", "1", "-W", "2", gateway], timeout=4)["ok"]
    return _test_result(
        "network",
        "passed",
        started,
        **wifi,
        gateway=gateway,
        gateway_reachable=reachable,
        gateway_check=(
            "reachable"
            if reachable is True
            else ("icmp_unavailable_or_filtered" if reachable is False else "not_tested")
        ),
    )


def _save_last_self_test(payload: dict) -> None:
    try:
        SELF_TEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = SELF_TEST_FILE.with_name(f".{SELF_TEST_FILE.name}.{os.getpid()}.tmp")
        with temporary.open("w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, indent=2)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, SELF_TEST_FILE)
    except OSError:
        pass


def get_last_self_test() -> dict | None:
    try:
        payload = json.loads(SELF_TEST_FILE.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else None
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def run_self_test() -> dict:
    if not _begin_hardware_operation():
        raise SelfTestBusyError("hardware_busy")
    _SELF_TEST_ACTIVE.set()
    started = time.monotonic()
    preview_paused = False
    try:
        recording = camera.get_recording_status()
        if recording.get("recording") or recording.get("finalizing"):
            raise SelfTestBusyError("recording_in_progress")
        # The board exposes one UVC image node. Temporarily release the idle
        # preview so the diagnostic capture can exercise that node itself.
        preview.pause()
        preview_paused = True
        checks = [
            _camera_capture_test(),
            _audio_test(),
            _storage_write_test(),
            _network_test(),
        ]
        failed = [item for item in checks if item["status"] == "failed"]
        warnings = [item for item in checks if item["status"] == "warning"]
        status = "passed" if not failed and not warnings else ("failed" if failed else "warning")
        payload = {
            "status": status,
            "completed_at": _utc_now(),
            "duration_ms": round((time.monotonic() - started) * 1000),
            "checks": checks,
        }
        _save_last_self_test(payload)
        return payload
    finally:
        if preview_paused:
            preview.resume()
        _SELF_TEST_ACTIVE.clear()
        _end_hardware_operation()


def sanitize_text(value: str, secrets: list[str] | None = None) -> str:
    sanitized = value
    for secret in secrets or []:
        if secret:
            sanitized = sanitized.replace(secret, "<redacted>")
    sanitized = _PRIVATE_KEY_RE.sub("<private-key-redacted>", sanitized)
    sanitized = _AUTH_HEADER_RE.sub(r"\1<redacted>", sanitized)
    return _SENSITIVE_VALUE_RE.sub(r"\1\2<redacted>", sanitized)


def _privacy_safe_health(health: dict) -> dict:
    def scrub(value, key: str = ""):
        if isinstance(value, dict):
            return {item_key: scrub(item, str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item, key) for item in value]
        if key.lower() == "ssid" and value:
            return "<redacted>"
        return copy.deepcopy(value)

    result = scrub(health)
    recording = result.get("recording")
    if isinstance(recording, dict) and recording.get("file"):
        recording["file"] = Path(str(recording["file"])).name
    return result


def _journal(unit: str) -> str:
    result = _command(
        [
            "/bin/journalctl", "--no-pager", "--output=short-iso",
            "-n", str(MAX_JOURNAL_LINES), "-u", unit,
        ],
        timeout=10,
    )
    return result["stdout"] or result["stderr"]


def build_diagnostic_bundle() -> tuple[bytes, str]:
    health = _privacy_safe_health(get_health())
    token = ""
    try:
        token = utils.get_api_token()
    except (OSError, ValueError):
        pass
    secrets = [token]
    generated = datetime.now(timezone.utc)
    filename = f"medicam-diagnostics-{generated.strftime('%Y%m%dT%H%M%SZ')}.zip"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest = {
            "format": 1,
            "generated_at": generated.isoformat(),
            "privacy": {
                "api_token_included": False,
                "wifi_password_included": False,
                "pairing_secret_included": False,
                "tls_private_key_included": False,
                "wifi_ssid_redacted": True,
                "log_bytes_per_source_limit": MAX_LOG_BYTES,
            },
        }
        archive.writestr("manifest.json", json.dumps(manifest, indent=2, sort_keys=True))
        archive.writestr(
            "health.json",
            sanitize_text(json.dumps(health, ensure_ascii=False, indent=2, sort_keys=True), secrets),
        )
        for name, path_factory in LOG_FILES.items():
            content = sanitize_text(_tail_file(path_factory()), secrets)
            archive.writestr(f"logs/{name}", content)
        for unit in SERVICE_UNITS:
            content = sanitize_text(_journal(unit)[-MAX_LOG_BYTES:], secrets)
            archive.writestr(f"logs/journal-{unit}.log", content)
        ip_command = shutil.which("ip") or "/usr/bin/ip"
        system_commands = {
            "usb-tree.txt": ["lsusb", "-t"],
            "network-addresses.txt": [ip_command, "-brief", "address"],
            "network-routes.txt": [ip_command, "route"],
        }
        for name, command in system_commands.items():
            result = _command(command)
            content = result["stdout"] or result["stderr"]
            archive.writestr(f"system/{name}", sanitize_text(content, secrets)[-MAX_LOG_BYTES:])
    return buffer.getvalue(), filename
