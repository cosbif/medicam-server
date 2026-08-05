"""Stable device, protocol, firmware, and uptime version reporting."""

from __future__ import annotations

import os
import platform
import subprocess
import time
from pathlib import Path

from app import bluetooth_provision


APP_PROTOCOL_VERSION = 4
MIN_APP_PROTOCOL_VERSION = 4
PROCESS_STARTED_MONOTONIC = time.monotonic()
REPO_DIR = Path(__file__).resolve().parents[1]
IMAGE_VERSION_FILE = Path(
    os.environ.get("MEDICAM_IMAGE_VERSION_FILE", "/etc/medicam/image-version")
)


def _command(command: list[str], timeout: float = 5) -> str:
    try:
        result = subprocess.run(
            command,
            cwd=REPO_DIR,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


SERVER_COMMIT = _command(["git", "rev-parse", "HEAD"])


def _release_tag() -> str | None:
    tag = _command(
        ["git", "describe", "--tags", "--exact-match", "--match", "medicam-v*", "HEAD"]
    )
    return tag or None


def _os_release() -> dict:
    values = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value.strip().strip('"')
    except OSError:
        pass
    return values


def _image_version() -> str:
    try:
        value = IMAGE_VERSION_FILE.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    os_release = _os_release()
    return "-".join(
        part
        for part in (
            os_release.get("ID", platform.system().lower()),
            os_release.get("VERSION_ID", platform.release()),
        )
        if part
    )


def _system_uptime_seconds() -> float:
    try:
        return float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
    except (OSError, ValueError, IndexError):
        return 0.0


def _camera_firmware() -> dict:
    device = _command(
        [
            "/bin/sh",
            "-c",
            "readlink -f /dev/v4l/by-id/*-video-index0 2>/dev/null | head -1",
        ]
    )
    if not device:
        return {"available": False, "firmware_revision": None}
    properties = _command(
        ["udevadm", "info", "--query=property", f"--name={device}"]
    )
    parsed = {}
    for line in properties.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            parsed[key] = value
    return {
        "available": True,
        "device": device,
        "vendor_id": parsed.get("ID_VENDOR_ID"),
        "model_id": parsed.get("ID_MODEL_ID"),
        "firmware_revision": parsed.get("ID_REVISION"),
        "driver": parsed.get("ID_USB_DRIVER") or "uvcvideo",
    }


def get_version_info() -> dict:
    service_uptime = max(0.0, time.monotonic() - PROCESS_STARTED_MONOTONIC)
    return {
        "server": {
            "commit": SERVER_COMMIT or None,
            "release": _release_tag(),
        },
        "protocols": {
            "app": APP_PROTOCOL_VERSION,
            "minimum_app": MIN_APP_PROTOCOL_VERSION,
            "ble": bluetooth_provision.PROTOCOL_VERSION,
        },
        "device_image": {
            "version": _image_version(),
            "os": _os_release().get("PRETTY_NAME", platform.platform()),
        },
        "camera": _camera_firmware(),
        "uptime_seconds": round(_system_uptime_seconds(), 1),
        "service_uptime_seconds": round(service_uptime, 1),
    }


def get_ping_version() -> dict:
    return {
        "commit": SERVER_COMMIT or None,
        "protocol": APP_PROTOCOL_VERSION,
    }
