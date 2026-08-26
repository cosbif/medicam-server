"""Outbound Medicam cloud enrollment, heartbeat, and bounded command agent.

Accepted commands have fixed empty parameter schemas. There is no shell, path,
log, video, release URL, branch, commit, or arbitrary command surface.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from app import camera, storage_manager, update_control, utils, version_info


DEFAULT_STATE_FILE = Path("/var/lib/medicam/cloud-state.json")
DEFAULT_BOOTSTRAP_TOKEN_FILE = Path("/etc/medicam/cloud-bootstrap-token")
MAX_RESPONSE_BYTES = 64 * 1024
MAX_STATE_BYTES = 16 * 1024
MAX_PENDING_COMMAND_RESULTS = 8
MAX_COMPLETED_COMMAND_IDS = 128


class CloudAgentError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class CloudAgentConfig:
    server_url: str
    device_id: str
    bootstrap_token: str
    state_file: Path
    ca_file: Path | None
    allow_signed_update: bool = False
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
            allow_signed_update=_environment_bool(
                "MEDICAM_CLOUD_ALLOW_SIGNED_UPDATE",
                False,
            ),
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


def _environment_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "1" if default else "0").strip().lower()
    if raw in {"1", "true", "yes"}:
        return True
    if raw in {"0", "false", "no"}:
        return False
    raise CloudAgentError(f"{name} must be a boolean")


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
            raise CloudAgentError(
                f"cloud API returned HTTP {error.code}",
                status_code=error.code,
            ) from error
        except urllib.error.URLError as error:
            raise CloudAgentError("cloud API is unavailable") from error
        except (TimeoutError, json.JSONDecodeError) as error:
            raise CloudAgentError("invalid cloud API response") from error


def collect_heartbeat() -> dict:
    version = version_info.get_version_info()
    recording = camera.get_persisted_recording_status()
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


def collect_diagnostics(heartbeat_provider: Callable[[], dict] = collect_heartbeat) -> dict:
    heartbeat = heartbeat_provider()
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "server_commit": heartbeat.get("server_commit"),
        "server_release": heartbeat.get("server_release"),
        "image_version": heartbeat.get("image_version"),
        "app_protocol": heartbeat.get("app_protocol"),
        "ble_protocol": heartbeat.get("ble_protocol"),
        "uptime_seconds": heartbeat.get("uptime_seconds"),
        "recording": heartbeat.get("recording"),
        "storage": heartbeat.get("storage"),
    }


def _parse_cloud_datetime(value: object, field: str) -> datetime:
    if not isinstance(value, str) or len(value) > 40:
        raise CloudAgentError(f"invalid command {field}")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise CloudAgentError(f"invalid command {field}") from error
    if parsed.tzinfo is None:
        raise CloudAgentError(f"invalid command {field}")
    return parsed.astimezone(timezone.utc)


def _bounded_optional_string(value: object, field: str, maximum: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > maximum:
        raise CloudAgentError(f"invalid diagnostics {field}")
    return value


def _bounded_integer(value: object, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CloudAgentError(f"invalid diagnostics {field}")
    if not minimum <= value <= maximum:
        raise CloudAgentError(f"invalid diagnostics {field}")
    return value


def _normalize_diagnostics(payload: object) -> dict:
    expected = {
        "generated_at",
        "server_commit",
        "server_release",
        "image_version",
        "app_protocol",
        "ble_protocol",
        "uptime_seconds",
        "recording",
        "storage",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise CloudAgentError("invalid diagnostics fields")

    generated_at = _parse_cloud_datetime(payload["generated_at"], "generated_at")
    uptime = payload["uptime_seconds"]
    if isinstance(uptime, bool) or not isinstance(uptime, (int, float)) or uptime < 0:
        raise CloudAgentError("invalid diagnostics uptime_seconds")

    recording = payload["recording"]
    if not isinstance(recording, dict) or set(recording) != {"active", "state"}:
        raise CloudAgentError("invalid diagnostics recording")
    if not isinstance(recording["active"], bool):
        raise CloudAgentError("invalid diagnostics recording.active")
    recording_state = recording["state"]
    if not isinstance(recording_state, str) or len(recording_state) > 40:
        raise CloudAgentError("invalid diagnostics recording.state")

    storage = payload["storage"]
    if not isinstance(storage, dict) or set(storage) != {
        "free_bytes",
        "critical_space",
    }:
        raise CloudAgentError("invalid diagnostics storage")
    free_bytes = storage["free_bytes"]
    if isinstance(free_bytes, bool) or not isinstance(free_bytes, int) or free_bytes < 0:
        raise CloudAgentError("invalid diagnostics storage.free_bytes")
    if not isinstance(storage["critical_space"], bool):
        raise CloudAgentError("invalid diagnostics storage.critical_space")

    return {
        "generated_at": generated_at.isoformat(),
        "server_commit": _bounded_optional_string(
            payload["server_commit"], "server_commit", 64
        ),
        "server_release": _bounded_optional_string(
            payload["server_release"], "server_release", 80
        ),
        "image_version": _bounded_optional_string(
            payload["image_version"], "image_version", 120
        ),
        "app_protocol": _bounded_integer(
            payload["app_protocol"], "app_protocol", 1, 10000
        ),
        "ble_protocol": _bounded_integer(
            payload["ble_protocol"], "ble_protocol", 1, 10000
        ),
        "uptime_seconds": float(uptime),
        "recording": {
            "active": recording["active"],
            "state": recording_state,
        },
        "storage": {
            "free_bytes": free_bytes,
            "critical_space": storage["critical_space"],
        },
    }


def _normalize_signed_update_start(payload: object) -> dict:
    if not isinstance(payload, dict):
        raise CloudAgentError("invalid signed update result")
    job_id = payload.get("job_id")
    if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}", job_id):
        raise CloudAgentError("invalid signed update job_id")
    if payload.get("state") != "queued":
        raise CloudAgentError("invalid signed update state")
    previous_commit = payload.get("previous_commit")
    if previous_commit is not None and (
        not isinstance(previous_commit, str)
        or not re.fullmatch(r"[0-9a-f]{40}", previous_commit)
    ):
        raise CloudAgentError("invalid signed update previous_commit")
    return {
        "accepted_at": datetime.now(timezone.utc).isoformat(),
        "job_id": job_id,
        "state": "queued",
        "release_channel": "signed-stable",
        "previous_commit": previous_commit,
    }


class CloudAgent:
    def __init__(
        self,
        config: CloudAgentConfig,
        *,
        client: CloudHttpClient | None = None,
        heartbeat_provider: Callable[[], dict] = collect_heartbeat,
        diagnostics_provider: Callable[[], dict] | None = None,
        update_starter: Callable[[], dict] = update_control.start_signed_update,
    ):
        config.validate()
        self.config = config
        self.client = client or CloudHttpClient(config)
        self.heartbeat_provider = heartbeat_provider
        self.diagnostics_provider = diagnostics_provider or (
            lambda: collect_diagnostics(self.heartbeat_provider)
        )
        self.update_starter = update_starter

    def _state(self) -> dict:
        state = _read_state(self.config.state_file)
        if not state:
            return {}
        if state.get("device_id") != self.config.device_id:
            raise CloudAgentError("cloud state belongs to another device")
        if state.get("server_url") != self.config.server_url:
            raise CloudAgentError("cloud state belongs to another server")
        return state

    def _device_token(self) -> str:
        state = self._state()
        if not state:
            return ""
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

    @staticmethod
    def _command_id(command: object) -> str:
        if not isinstance(command, dict):
            raise CloudAgentError("cloud command is not an object")
        command_id = command.get("id")
        if not isinstance(command_id, str) or len(command_id) != 36:
            raise CloudAgentError("cloud command has an invalid ID")
        try:
            parsed = uuid.UUID(command_id)
        except ValueError as error:
            raise CloudAgentError("cloud command has an invalid ID") from error
        if str(parsed) != command_id:
            raise CloudAgentError("cloud command has a non-canonical ID")
        return command_id

    @staticmethod
    def _validate_command(command: dict) -> None:
        expected = {
            "id",
            "command_type",
            "parameters",
            "created_at",
            "expires_at",
        }
        if set(command) != expected:
            raise CloudAgentError("cloud command has unexpected fields")
        if command["command_type"] not in {
            "collect_diagnostics",
            "start_signed_update",
        }:
            raise CloudAgentError("unsupported cloud command")
        if command["parameters"] != {}:
            raise CloudAgentError("cloud command parameters must be empty")
        created_at = _parse_cloud_datetime(command["created_at"], "created_at")
        expires_at = _parse_cloud_datetime(command["expires_at"], "expires_at")
        ttl = expires_at - created_at
        if not timedelta(seconds=30) <= ttl <= timedelta(minutes=15):
            raise CloudAgentError("cloud command has an invalid TTL")
        if expires_at <= datetime.now(timezone.utc):
            raise CloudAgentError("cloud command has expired")

    def _execute_command(self, command: dict) -> dict:
        try:
            self._validate_command(command)
        except CloudAgentError:
            return {
                "status": "failed",
                "diagnostics": None,
                "signed_update": None,
                "error_code": "invalid_command",
            }
        if command["command_type"] == "collect_diagnostics":
            try:
                diagnostics = _normalize_diagnostics(self.diagnostics_provider())
            except Exception:
                return {
                    "status": "failed",
                    "diagnostics": None,
                    "signed_update": None,
                    "error_code": "diagnostics_unavailable",
                }
            return {
                "status": "succeeded",
                "diagnostics": diagnostics,
                "signed_update": None,
                "error_code": None,
            }
        if not self.config.allow_signed_update:
            return {
                "status": "failed",
                "diagnostics": None,
                "signed_update": None,
                "error_code": "signed_update_disabled",
            }
        try:
            signed_update = _normalize_signed_update_start(self.update_starter())
        except update_control.UpdateStartBlockedError as error:
            allowed_codes = {
                "recording_in_progress",
                "update_in_progress",
                "device_busy",
            }
            return {
                "status": "failed",
                "diagnostics": None,
                "signed_update": None,
                "error_code": (
                    error.code if error.code in allowed_codes else "update_start_failed"
                ),
            }
        except Exception:
            return {
                "status": "failed",
                "diagnostics": None,
                "signed_update": None,
                "error_code": "update_start_failed",
            }
        return {
            "status": "succeeded",
            "diagnostics": None,
            "signed_update": signed_update,
            "error_code": None,
        }

    @staticmethod
    def _completed_command_ids(state: dict) -> list[str]:
        values = state.get("completed_command_ids", [])
        if not isinstance(values, list) or any(
            not isinstance(value, str) for value in values
        ):
            raise CloudAgentError("invalid completed command journal")
        return values[-MAX_COMPLETED_COMMAND_IDS:]

    @staticmethod
    def _pending_command_results(state: dict) -> list[dict]:
        values = state.get("pending_command_results", [])
        if not isinstance(values, list) or any(
            not isinstance(value, dict)
            or set(value) != {"id", "payload"}
            or not isinstance(value.get("id"), str)
            or not isinstance(value.get("payload"), dict)
            for value in values
        ):
            raise CloudAgentError("invalid pending command journal")
        if len(values) > MAX_PENDING_COMMAND_RESULTS:
            raise CloudAgentError("pending command journal is too large")
        return values

    def _record_pending_result(
        self,
        command_id: str,
        payload: dict,
    ) -> None:
        state = self._state()
        pending = self._pending_command_results(state)
        if any(receipt["id"] == command_id for receipt in pending):
            return
        if len(pending) >= MAX_PENDING_COMMAND_RESULTS:
            raise CloudAgentError("pending command journal is full")
        state["pending_command_results"] = [
            *pending,
            {"id": command_id, "payload": payload},
        ]
        _write_state(self.config.state_file, state)

    def _submit_pending_results(self, token: str) -> None:
        while True:
            state = self._state()
            pending = self._pending_command_results(state)
            if not pending:
                return
            receipt = pending[0]
            command_id = self._command_id(receipt)
            self.client.post(
                f"/api/v1/device/commands/{command_id}/result",
                receipt["payload"],
                token,
            )
            completed = self._completed_command_ids(state)
            if command_id not in completed:
                completed.append(command_id)
            state["completed_command_ids"] = completed[-MAX_COMPLETED_COMMAND_IDS:]
            state["pending_command_results"] = pending[1:]
            _write_state(self.config.state_file, state)

    def _process_one_command(self, token: str) -> None:
        # A result is persisted before transmission. Retrying it first makes a
        # network failure unable to trigger a second diagnostics collection.
        self._submit_pending_results(token)
        response = self.client.post("/api/v1/device/commands/poll", {}, token)
        if set(response) != {"command", "server_time"}:
            raise CloudAgentError("invalid cloud command poll response")
        command = response["command"]
        if command is None:
            return
        command_id = self._command_id(command)
        state = self._state()
        if command_id in self._completed_command_ids(state):
            return
        result = self._execute_command(command)
        self._record_pending_result(command_id, result)
        self._submit_pending_results(token)

    def run_once(self) -> dict:
        token = self._device_token() or self.enroll()
        heartbeat_response = self.client.post(
            "/api/v1/device/heartbeat",
            self.heartbeat_provider(),
            token,
        )
        self._process_one_command(token)
        return heartbeat_response

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
