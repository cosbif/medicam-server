"""Outbound Medicam cloud enrollment and heartbeat agent.

The agent deliberately has no command execution surface. Its first protocol
revision only enrolls one device credential and publishes a bounded technical
status payload over HTTPS.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from app import camera, storage_manager, utils, version_info


DEFAULT_STATE_FILE = Path("/var/lib/medicam/cloud-state.json")
DEFAULT_BOOTSTRAP_TOKEN_FILE = Path("/etc/medicam/cloud-bootstrap-token")
MAX_RESPONSE_BYTES = 64 * 1024
MAX_STATE_BYTES = 16 * 1024


class CloudAgentError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudAgentConfig:
    server_url: str
    device_id: str
    bootstrap_token: str
    state_file: Path
    ca_file: Path | None
    request_timeout_seconds: float = 15.0
    heartbeat_seconds: int = 30

    @classmethod
    def from_environment(cls) -> "CloudAgentConfig":
        bootstrap_token = os.environ.get(
            "MEDICAM_CLOUD_BOOTSTRAP_TOKEN",
            "",
        ).strip()
        if not bootstrap_token:
            token_file = Path(
                os.environ.get(
                    "MEDICAM_CLOUD_BOOTSTRAP_TOKEN_FILE",
                    str(DEFAULT_BOOTSTRAP_TOKEN_FILE),
                )
            )
            bootstrap_token = _read_secret_file(token_file)

        ca_value = os.environ.get("MEDICAM_CLOUD_CA_FILE", "").strip()
        cloud_device_id = os.environ.get("MEDICAM_CLOUD_DEVICE_ID", "").strip()
        return cls(
            server_url=os.environ.get("MEDICAM_CLOUD_URL", "").strip().rstrip("/"),
            # The override is intended for the Mac simulator and manufacturing
            # tests. Production devices derive the ID from their stable board
            # identity through utils.get_device_id().
            device_id=cloud_device_id or utils.get_device_id(),
            bootstrap_token=bootstrap_token,
            state_file=Path(
                os.environ.get(
                    "MEDICAM_CLOUD_STATE_FILE",
                    str(DEFAULT_STATE_FILE),
                )
            ),
            ca_file=Path(ca_value) if ca_value else None,
            request_timeout_seconds=_environment_float(
                "MEDICAM_CLOUD_REQUEST_TIMEOUT_SECONDS",
                15.0,
                minimum=2.0,
                maximum=120.0,
            ),
            heartbeat_seconds=_environment_int(
                "MEDICAM_CLOUD_HEARTBEAT_SECONDS",
                30,
                minimum=15,
                maximum=3600,
            ),
        )

    def validate(self) -> None:
        if not self.server_url:
            raise CloudAgentError("MEDICAM_CLOUD_URL is not configured")
        parsed = urllib.parse.urlparse(self.server_url)
        local_hosts = {"127.0.0.1", "localhost", "::1"}
        if parsed.scheme != "https" and not (
            parsed.scheme == "http" and parsed.hostname in local_hosts
        ):
            raise CloudAgentError(
                "cloud URL must use HTTPS; HTTP is allowed only for a local simulator"
            )
        if parsed.username or parsed.password or not parsed.hostname:
            raise CloudAgentError("cloud URL must not contain credentials")
        if not self.device_id or len(self.device_id) > 32:
            raise CloudAgentError("invalid device ID")
        if self.ca_file is not None and not self.ca_file.is_file():
            raise CloudAgentError("configured cloud CA file does not exist")


def _environment_int(name: str, default: int, *, minimum: int, maximum: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as error:
        raise CloudAgentError(f"{name} must be an integer") from error
    if not minimum <= value <= maximum:
        raise CloudAgentError(f"{name} must be between {minimum} and {maximum}")
    return value


def _environment_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as error:
        raise CloudAgentError(f"{name} must be numeric") from error
    if not minimum <= value <= maximum:
        raise CloudAgentError(f"{name} must be between {minimum} and {maximum}")
    return value


def _read_secret_file(path: Path) -> str:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(metadata.st_mode)
                or metadata.st_size > 1024
                or metadata.st_mode & 0o077
            ):
                raise CloudAgentError("unsafe cloud bootstrap token file")
            return os.read(descriptor, 1025).decode("ascii").strip()
        finally:
            os.close(descriptor)
    except FileNotFoundError:
        return ""
    except UnicodeDecodeError as error:
        raise CloudAgentError("cloud bootstrap token must be ASCII") from error


def _read_state(path: Path) -> dict:
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_STATE_BYTES:
                raise CloudAgentError("unsafe cloud state file")
            if metadata.st_mode & 0o077:
                os.fchmod(descriptor, 0o600)
            raw = os.read(descriptor, MAX_STATE_BYTES + 1)
        finally:
            os.close(descriptor)
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise CloudAgentError("cannot read cloud state") from error


def _write_state(path: Path, payload: dict) -> None:
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
            0o600,
            dir_fd=directory,
        )
        encoded = json.dumps(payload, sort_keys=True).encode("utf-8")
        if len(encoded) > MAX_STATE_BYTES:
            raise CloudAgentError("cloud state is too large")
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, encoded)
        os.fsync(descriptor)
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


class CloudHttpClient:
    def __init__(self, config: CloudAgentConfig):
        self.config = config
        self.ssl_context = ssl.create_default_context(
            cafile=str(config.ca_file) if config.ca_file else None
        )

    def post(self, path: str, payload: dict, token: str) -> dict:
        if not path.startswith("/"):
            raise CloudAgentError("cloud API path must be absolute")
        request = urllib.request.Request(
            f"{self.config.server_url}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "medicam-device-agent/1",
            },
            method="POST",
        )
        try:
            context = (
                self.ssl_context
                if urllib.parse.urlparse(self.config.server_url).scheme == "https"
                else None
            )
            with urllib.request.urlopen(
                request,
                timeout=self.config.request_timeout_seconds,
                context=context,
            ) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise CloudAgentError("cloud response is too large")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise CloudAgentError("cloud response is not an object")
                return result
        except urllib.error.HTTPError as error:
            raise CloudAgentError(f"cloud API returned HTTP {error.code}") from error
        except urllib.error.URLError as error:
            raise CloudAgentError("cloud API is unavailable") from error
        except (TimeoutError, json.JSONDecodeError) as error:
            raise CloudAgentError("invalid cloud API response") from error


def collect_heartbeat() -> dict:
    version = version_info.get_version_info()
    recording = camera.get_recording_status()
    storage = storage_manager.get_storage_info()
    protocols = version.get("protocols", {})
    server = version.get("server", {})
    image = version.get("device_image", {})
    return {
        "device_name": utils.get_device_name(),
        "server_commit": server.get("commit"),
        "server_release": server.get("release"),
        "image_version": image.get("version"),
        "app_protocol": int(protocols.get("app") or 1),
        "ble_protocol": int(protocols.get("ble") or 1),
        "uptime_seconds": max(0.0, float(version.get("uptime_seconds") or 0.0)),
        "recording": {
            "active": bool(recording.get("recording")),
            "state": str(recording.get("state") or "unknown")[:40],
        },
        "storage": {
            "free_bytes": max(0, int(storage.get("free_bytes") or 0)),
            "critical_space": bool(storage.get("critical_space")),
        },
    }


class CloudAgent:
    def __init__(
        self,
        config: CloudAgentConfig,
        *,
        client: CloudHttpClient | None = None,
        heartbeat_provider: Callable[[], dict] = collect_heartbeat,
    ):
        config.validate()
        self.config = config
        self.client = client or CloudHttpClient(config)
        self.heartbeat_provider = heartbeat_provider

    def _device_token(self) -> str:
        state = _read_state(self.config.state_file)
        if not state:
            return ""
        if state.get("device_id") != self.config.device_id:
            raise CloudAgentError("cloud state belongs to another device")
        if state.get("server_url") != self.config.server_url:
            raise CloudAgentError("cloud state belongs to another server")
        token = state.get("device_token")
        if not isinstance(token, str) or len(token) < 32:
            raise CloudAgentError("cloud state has no valid device credential")
        return token

    def enroll(self) -> str:
        existing = self._device_token()
        if existing:
            return existing
        if not self.config.bootstrap_token:
            raise CloudAgentError("cloud enrollment requires a bootstrap token")
        response = self.client.post(
            "/api/v1/device/enroll",
            {"device_id": self.config.device_id},
            self.config.bootstrap_token,
        )
        if response.get("device_id") != self.config.device_id:
            raise CloudAgentError("cloud enrolled an unexpected device")
        device_token = response.get("device_token")
        if not isinstance(device_token, str) or len(device_token) < 32:
            raise CloudAgentError("cloud returned an invalid device credential")
        _write_state(
            self.config.state_file,
            {
                "device_id": self.config.device_id,
                "device_token": device_token,
                "server_url": self.config.server_url,
                "enrolled_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return device_token

    def run_once(self) -> dict:
        token = self._device_token() or self.enroll()
        return self.client.post(
            "/api/v1/device/heartbeat",
            self.heartbeat_provider(),
            token,
        )

    def run_forever(self) -> None:
        failures = 0
        while True:
            try:
                response = self.run_once()
                failures = 0
                delay = response.get("next_heartbeat_seconds")
                if not isinstance(delay, int):
                    delay = self.config.heartbeat_seconds
                delay = max(15, min(delay, 3600))
                print(f"cloud heartbeat accepted; next={delay}s", flush=True)
            except CloudAgentError as error:
                failures += 1
                delay = min(
                    self.config.heartbeat_seconds * (2 ** min(failures - 1, 5)),
                    900,
                )
                print(f"cloud heartbeat failed: {error}; retry={delay}s", flush=True)
            time.sleep(delay)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Medicam outbound cloud agent")
    parser.add_argument(
        "--once",
        action="store_true",
        help="enroll if necessary, send one heartbeat, and exit",
    )
    arguments = parser.parse_args(argv)
    try:
        config = CloudAgentConfig.from_environment()
        if not config.server_url:
            print("Medicam cloud agent is disabled: MEDICAM_CLOUD_URL is empty")
            return 0
        agent = CloudAgent(config)
        if arguments.once:
            response = agent.run_once()
            print(json.dumps(response, sort_keys=True))
        else:
            agent.run_forever()
        return 0
    except CloudAgentError as error:
        print(f"Medicam cloud agent error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
