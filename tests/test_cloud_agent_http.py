from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import tempfile
from threading import Thread
import unittest
from pathlib import Path

from app.cloud_agent import CloudAgent, CloudAgentConfig, _read_state


BOOTSTRAP_TOKEN = "bootstrap-" + "b" * 40
DEVICE_TOKEN = "device-" + "d" * 40


class CloudContractHandler(BaseHTTPRequestHandler):
    records: list[tuple[str, dict, str]] = []

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length))
        authorization = self.headers.get("Authorization", "")
        self.records.append((self.path, payload, authorization))

        if self.path == "/api/v1/device/enroll":
            if authorization != f"Bearer {BOOTSTRAP_TOKEN}":
                self.send_error(401)
                return
            response = {
                "device_id": "MACSIM01",
                "device_token": DEVICE_TOKEN,
                "heartbeat_path": "/api/v1/device/heartbeat",
            }
        elif self.path == "/api/v1/device/heartbeat":
            if authorization != f"Bearer {DEVICE_TOKEN}":
                self.send_error(401)
                return
            response = {
                "accepted_at": "2026-08-22T12:00:00Z",
                "next_heartbeat_seconds": 30,
            }
        elif self.path == "/api/v1/device/commands/poll":
            if authorization != f"Bearer {DEVICE_TOKEN}":
                self.send_error(401)
                return
            response = {
                "command": None,
                "server_time": "2026-08-22T12:00:00Z",
            }
        else:
            self.send_error(404)
            return

        encoded = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, format: str, *args) -> None:
        del format, args


class CloudAgentHttpContractTests(unittest.TestCase):
    def setUp(self):
        CloudContractHandler.records = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), CloudContractHandler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.temporary = tempfile.TemporaryDirectory()
        self.state_file = Path(self.temporary.name) / "cloud-state.json"
        self.heartbeat = {
            "device_name": "Medicam-ACSIM01",
            "server_commit": None,
            "server_release": None,
            "image_version": "mac-simulator",
            "app_protocol": 4,
            "ble_protocol": 4,
            "uptime_seconds": 1.0,
            "recording": {"active": False, "state": "idle"},
            "storage": {"free_bytes": 1024, "critical_space": False},
        }

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def config(self, bootstrap_token: str) -> CloudAgentConfig:
        host, port = self.server.server_address
        return CloudAgentConfig(
            server_url=f"http://{host}:{port}",
            device_id="MACSIM01",
            bootstrap_token=bootstrap_token,
            state_file=self.state_file,
            ca_file=None,
        )

    def test_mac_agent_enrolls_over_http_then_reuses_persisted_credential(self):
        first = CloudAgent(
            self.config(BOOTSTRAP_TOKEN),
            heartbeat_provider=lambda: self.heartbeat,
        ).run_once()
        second = CloudAgent(
            self.config(""),
            heartbeat_provider=lambda: self.heartbeat,
        ).run_once()

        self.assertEqual(first["next_heartbeat_seconds"], 30)
        self.assertEqual(second["next_heartbeat_seconds"], 30)
        self.assertEqual(
            [record[0] for record in CloudContractHandler.records],
            [
                "/api/v1/device/enroll",
                "/api/v1/device/heartbeat",
                "/api/v1/device/commands/poll",
                "/api/v1/device/heartbeat",
                "/api/v1/device/commands/poll",
            ],
        )
        saved = _read_state(self.state_file)
        self.assertEqual(saved["device_token"], DEVICE_TOKEN)
        self.assertEqual(self.state_file.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(BOOTSTRAP_TOKEN, self.state_file.read_text())


if __name__ == "__main__":
    unittest.main()
