from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.cloud_agent import (
    CloudAgent,
    CloudAgentConfig,
    CloudAgentError,
    _read_state,
)


class FakeCloudClient:
    def __init__(self, command=None):
        self.calls = []
        self.command = command

    def post(self, path: str, payload: dict, token: str) -> dict:
        self.calls.append((path, payload, token))
        if path == "/api/v1/device/enroll":
            return {
                "device_id": payload["device_id"],
                "device_token": "device-token-" + "x" * 32,
            }
        if path == "/api/v1/device/commands/poll":
            return {
                "command": self.command,
                "server_time": "2026-08-21T10:00:00Z",
            }
        if path.endswith("/result"):
            return {"state": payload["status"]}
        return {
            "accepted_at": "2026-01-01T00:00:00Z",
            "next_heartbeat_seconds": 30,
        }


class CloudAgentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temporary.name) / "cloud-state.json"
        self.config = CloudAgentConfig(
            server_url="https://cloud.example.com",
            device_id="ABCD1234",
            bootstrap_token="bootstrap-" + "b" * 32,
            state_file=self.state_file,
            ca_file=None,
        )
        self.heartbeat = {
            "device_name": "Medicam-CD1234",
            "server_commit": None,
            "server_release": None,
            "image_version": "mac-simulator",
            "app_protocol": 4,
            "ble_protocol": 4,
            "uptime_seconds": 1.0,
            "recording": {"active": False, "state": "idle"},
            "storage": {"free_bytes": 1000, "critical_space": False},
        }

    def tearDown(self):
        self.temporary.cleanup()

    def test_first_run_enrolls_then_sends_heartbeat(self):
        client = FakeCloudClient()
        agent = CloudAgent(
            self.config,
            client=client,
            heartbeat_provider=lambda: self.heartbeat,
        )
        result = agent.run_once()

        self.assertEqual(result["next_heartbeat_seconds"], 30)
        self.assertEqual(
            [call[0] for call in client.calls],
            [
                "/api/v1/device/enroll",
                "/api/v1/device/heartbeat",
                "/api/v1/device/commands/poll",
            ],
        )
        saved = _read_state(self.state_file)
        self.assertEqual(saved["device_id"], "ABCD1234")
        self.assertTrue(saved["device_token"].startswith("device-token-"))
        self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)

    def test_subsequent_run_uses_saved_device_token(self):
        first_client = FakeCloudClient()
        CloudAgent(
            self.config,
            client=first_client,
            heartbeat_provider=lambda: self.heartbeat,
        ).run_once()

        second_client = FakeCloudClient()
        second_config = CloudAgentConfig(
            **{
                **self.config.__dict__,
                "bootstrap_token": "",
            }
        )
        CloudAgent(
            second_config,
            client=second_client,
            heartbeat_provider=lambda: self.heartbeat,
        ).run_once()

        self.assertEqual(
            [call[0] for call in second_client.calls],
            ["/api/v1/device/heartbeat", "/api/v1/device/commands/poll"],
        )

    def test_diagnostics_command_is_bounded_and_acknowledged(self):
        command_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        client = FakeCloudClient(
            command={
                "id": command_id,
                "command_type": "collect_diagnostics",
                "parameters": {},
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=2)).isoformat(),
            }
        )
        diagnostics_calls = []
        diagnostics = {
            "generated_at": now.isoformat(),
            "server_commit": "a" * 40,
            "server_release": "medicam-v1.2.29",
            "image_version": "mac-simulator",
            "app_protocol": 4,
            "ble_protocol": 4,
            "uptime_seconds": 1.0,
            "recording": {"active": False, "state": "idle"},
            "storage": {"free_bytes": 1000, "critical_space": False},
        }
        agent = CloudAgent(
            self.config,
            client=client,
            heartbeat_provider=lambda: self.heartbeat,
            diagnostics_provider=lambda: diagnostics_calls.append(1) or diagnostics,
        )
        agent.run_once()

        self.assertEqual(diagnostics_calls, [1])
        result_call = next(call for call in client.calls if call[0].endswith("/result"))
        self.assertEqual(
            result_call[0],
            f"/api/v1/device/commands/{command_id}/result",
        )
        self.assertEqual(result_call[1]["status"], "succeeded")
        self.assertEqual(set(result_call[1]["diagnostics"]), set(diagnostics))
        saved = _read_state(self.state_file)
        self.assertEqual(saved["completed_command_ids"], [command_id])
        self.assertEqual(saved["pending_command_results"], [])

    def test_signed_update_start_is_opt_in_bounded_and_acknowledged(self):
        command_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        client = FakeCloudClient(
            command={
                "id": command_id,
                "command_type": "start_signed_update",
                "parameters": {},
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=5)).isoformat(),
            }
        )
        starts = []
        config = CloudAgentConfig(
            **{**self.config.__dict__, "allow_signed_update": True}
        )
        CloudAgent(
            config,
            client=client,
            heartbeat_provider=lambda: self.heartbeat,
            update_starter=lambda: starts.append(1)
            or {
                "job_id": "a" * 32,
                "state": "queued",
                "previous_commit": "b" * 40,
                "message": "must not leave the camera",
                "target_tag": None,
            },
        ).run_once()

        self.assertEqual(starts, [1])
        result = next(call[1] for call in client.calls if call[0].endswith("/result"))
        self.assertEqual(result["status"], "succeeded")
        self.assertIsNone(result["diagnostics"])
        self.assertEqual(result["signed_update"]["job_id"], "a" * 32)
        self.assertEqual(result["signed_update"]["state"], "queued")
        self.assertEqual(
            result["signed_update"]["release_channel"],
            "signed-stable",
        )
        self.assertEqual(result["signed_update"]["previous_commit"], "b" * 40)
        self.assertNotIn("message", result["signed_update"])
        self.assertNotIn("target_tag", result["signed_update"])

    def test_signed_update_is_disabled_for_mac_simulator_by_default(self):
        now = datetime.now(timezone.utc)
        client = FakeCloudClient(
            command={
                "id": str(uuid.uuid4()),
                "command_type": "start_signed_update",
                "parameters": {},
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=2)).isoformat(),
            }
        )
        starts = []
        CloudAgent(
            self.config,
            client=client,
            heartbeat_provider=lambda: self.heartbeat,
            update_starter=lambda: starts.append(1) or {},
        ).run_once()

        result = next(call[1] for call in client.calls if call[0].endswith("/result"))
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "signed_update_disabled")
        self.assertEqual(starts, [])

    def test_signed_update_result_retry_does_not_start_update_twice(self):
        command_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        command = {
            "id": command_id,
            "command_type": "start_signed_update",
            "parameters": {},
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=5)).isoformat(),
        }

        class FailFirstResultClient(FakeCloudClient):
            def __init__(self):
                super().__init__(command=command)
                self.fail_result = True

            def post(self, path: str, payload: dict, token: str) -> dict:
                if path.endswith("/result") and self.fail_result:
                    self.calls.append((path, payload, token))
                    self.fail_result = False
                    raise CloudAgentError("simulated network failure")
                return super().post(path, payload, token)

        starts = []
        client = FailFirstResultClient()
        config = CloudAgentConfig(
            **{**self.config.__dict__, "allow_signed_update": True}
        )
        agent = CloudAgent(
            config,
            client=client,
            heartbeat_provider=lambda: self.heartbeat,
            update_starter=lambda: starts.append(1)
            or {
                "job_id": "c" * 32,
                "state": "queued",
                "previous_commit": None,
            },
        )
        with self.assertRaisesRegex(CloudAgentError, "network failure"):
            agent.run_once()
        agent.run_once()

        self.assertEqual(starts, [1])
        self.assertEqual(
            sum(call[0].endswith("/result") for call in client.calls),
            2,
        )

    def test_failed_result_upload_is_retried_without_reexecution(self):
        command_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        command = {
            "id": command_id,
            "command_type": "collect_diagnostics",
            "parameters": {},
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=2)).isoformat(),
        }

        class FailFirstResultClient(FakeCloudClient):
            def __init__(self):
                super().__init__(command=command)
                self.fail_result = True

            def post(self, path: str, payload: dict, token: str) -> dict:
                if path.endswith("/result") and self.fail_result:
                    self.calls.append((path, payload, token))
                    self.fail_result = False
                    raise CloudAgentError("simulated network failure")
                return super().post(path, payload, token)

        client = FailFirstResultClient()
        diagnostics_calls = []
        agent = CloudAgent(
            self.config,
            client=client,
            heartbeat_provider=lambda: self.heartbeat,
            diagnostics_provider=lambda: diagnostics_calls.append(1)
            or {
                "generated_at": now.isoformat(),
                "server_commit": None,
                "server_release": None,
                "image_version": "mac-simulator",
                "app_protocol": 4,
                "ble_protocol": 4,
                "uptime_seconds": 1.0,
                "recording": {"active": False, "state": "idle"},
                "storage": {"free_bytes": 1000, "critical_space": False},
            },
        )
        with self.assertRaisesRegex(CloudAgentError, "network failure"):
            agent.run_once()
        self.assertEqual(len(_read_state(self.state_file)["pending_command_results"]), 1)

        agent.run_once()
        self.assertEqual(diagnostics_calls, [1])
        self.assertEqual(_read_state(self.state_file)["pending_command_results"], [])
        self.assertEqual(
            sum(call[0].endswith("/result") for call in client.calls),
            2,
        )

    def test_arbitrary_and_expired_commands_are_never_executed(self):
        now = datetime.now(timezone.utc)
        diagnostics_calls = []
        commands = (
            {
                "id": str(uuid.uuid4()),
                "command_type": "run_shell",
                "parameters": {"command": "cat /etc/shadow"},
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=2)).isoformat(),
            },
            {
                "id": str(uuid.uuid4()),
                "command_type": "collect_diagnostics",
                "parameters": {},
                "created_at": (now - timedelta(minutes=2)).isoformat(),
                "expires_at": (now - timedelta(seconds=1)).isoformat(),
            },
            {
                "id": str(uuid.uuid4()),
                "command_type": "start_signed_update",
                "parameters": {"tag": "medicam-v999.0.0"},
                "created_at": now.isoformat(),
                "expires_at": (now + timedelta(minutes=2)).isoformat(),
            },
        )
        for command in commands:
            state_file = Path(self.temporary.name) / f"{command['id']}.json"
            config = CloudAgentConfig(**{**self.config.__dict__, "state_file": state_file})
            client = FakeCloudClient(command=command)
            CloudAgent(
                config,
                client=client,
                heartbeat_provider=lambda: self.heartbeat,
                diagnostics_provider=lambda: diagnostics_calls.append(1) or {},
            ).run_once()
            result = next(call[1] for call in client.calls if call[0].endswith("/result"))
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_code"], "invalid_command")
        self.assertEqual(diagnostics_calls, [])

    def test_state_cannot_be_reused_for_another_server(self):
        client = FakeCloudClient()
        CloudAgent(
            self.config,
            client=client,
            heartbeat_provider=lambda: self.heartbeat,
        ).run_once()
        other = CloudAgentConfig(
            **{
                **self.config.__dict__,
                "server_url": "https://other.example.com",
            }
        )
        with self.assertRaisesRegex(CloudAgentError, "another server"):
            CloudAgent(
                other,
                client=FakeCloudClient(),
                heartbeat_provider=lambda: self.heartbeat,
            ).run_once()

    def test_non_local_plain_http_is_rejected(self):
        config = CloudAgentConfig(
            **{
                **self.config.__dict__,
                "server_url": "http://cloud.example.com",
            }
        )
        with self.assertRaisesRegex(CloudAgentError, "HTTPS"):
            CloudAgent(config, client=FakeCloudClient())

    def test_local_http_is_allowed_for_mac_simulator(self):
        config = CloudAgentConfig(
            **{
                **self.config.__dict__,
                "server_url": "http://127.0.0.1:8000",
            }
        )
        CloudAgent(config, client=FakeCloudClient())

    def test_read_state_rejects_symlink(self):
        target = Path(self.temporary.name) / "target.json"
        target.write_text(json.dumps({"device_token": "secret"}), encoding="utf-8")
        self.state_file.symlink_to(target)
        with self.assertRaises(CloudAgentError):
            _read_state(self.state_file)
        self.assertTrue(stat.S_ISLNK(os.lstat(self.state_file).st_mode))


if __name__ == "__main__":
    unittest.main()
