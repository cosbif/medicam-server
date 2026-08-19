from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from app.cloud_agent import (
    CloudAgent,
    CloudAgentConfig,
    CloudAgentError,
    _read_state,
)


class FakeCloudClient:
    def __init__(self):
        self.calls = []

    def post(self, path: str, payload: dict, token: str) -> dict:
        self.calls.append((path, payload, token))
        if path == "/api/v1/device/enroll":
            return {
                "device_id": payload["device_id"],
                "device_token": "device-token-" + "x" * 32,
            }
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
            ["/api/v1/device/enroll", "/api/v1/device/heartbeat"],
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
            ["/api/v1/device/heartbeat"],
        )

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
