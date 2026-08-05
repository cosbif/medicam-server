import io
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app import diagnostics


class DiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.self_test_file = Path(self.tmp.name) / "self-test.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_health_aggregates_required_product_status(self):
        recording = {
            "recording": False,
            "interrupted": False,
            "camera": {"available": True, "device": "/dev/video0"},
        }
        service = {"ok": True, "active": "active", "sub": "running", "enabled": "enabled"}
        with patch("app.diagnostics.camera.get_recording_status", return_value=recording), patch(
            "app.diagnostics.camera.get_settings",
            return_value={"audio_enabled": True, "audio_device": "auto"},
        ), patch("app.diagnostics.audio.list_capture_devices", return_value=[{"id": "mic"}]), patch(
            "app.diagnostics.audio.resolve_capture_device", return_value={"id": "mic"}
        ), patch(
            "app.diagnostics.storage_manager.get_storage_info",
            return_value={"critical_space": False, "low_space": False},
        ), patch(
            "app.diagnostics._wifi_health",
            return_value={"connected": True, "ssid": "Home", "ip": "192.168.1.2"},
        ), patch("app.diagnostics._service_status", return_value=service), patch(
            "app.diagnostics.get_usb_topology",
            return_value={"chain": [{"power_control": "on"}], "power_protected": True},
        ), patch(
            "app.diagnostics._usb_error_summary", return_value={"detected_count": 0, "recent": []}
        ), patch("app.diagnostics.version_info.get_version_info", return_value={"server": {}}), patch(
            "app.diagnostics.updater.get_update_status", return_value={"state": "idle"}
        ), patch("app.diagnostics.get_last_self_test", return_value=None), patch(
            "app.diagnostics._thermal_status", return_value=[]
        ):
            health = diagnostics.get_health()

        self.assertEqual(health["status"], "healthy")
        self.assertTrue(health["wifi"]["connected"])
        self.assertTrue(health["camera"]["available"])
        self.assertTrue(health["microphone"]["found"])
        self.assertIn("medicam.service", health["services"])

    def test_self_test_runs_all_checks_and_persists_result(self):
        passed = lambda name: {"name": name, "status": "passed", "duration_ms": 1}
        with patch.object(diagnostics, "SELF_TEST_FILE", self.self_test_file), patch(
            "app.diagnostics.camera.get_recording_status", return_value={"recording": False}
        ), patch("app.diagnostics._camera_capture_test", return_value=passed("camera")), patch(
            "app.diagnostics._audio_test", return_value=passed("audio")
        ), patch("app.diagnostics._storage_write_test", return_value=passed("storage")), patch(
            "app.diagnostics._network_test", return_value=passed("network")
        ):
            result = diagnostics.run_self_test()

        self.assertEqual(result["status"], "passed")
        self.assertEqual(len(result["checks"]), 4)
        self.assertEqual(json.loads(self.self_test_file.read_text())["status"], "passed")
        self.assertEqual(self.self_test_file.stat().st_mode & 0o777, 0o600)

    def test_self_test_refuses_to_open_hardware_during_recording(self):
        with patch(
            "app.diagnostics.camera.get_recording_status",
            return_value={"recording": True, "finalizing": False},
        ):
            with self.assertRaises(diagnostics.SelfTestBusyError) as context:
                diagnostics.run_self_test()
        self.assertEqual(str(context.exception), "recording_in_progress")

    def test_bundle_redacts_tokens_passwords_private_keys_and_ssid(self):
        log = Path(self.tmp.name) / "ffmpeg.log"
        token = "A" * 48
        log.write_text(
            f"x-medicam-token={token}\npassword=hunter2\n"
            "-----BEGIN PRIVATE KEY-----\nprivate\n-----END PRIVATE KEY-----\n",
            encoding="utf-8",
        )
        health = {
            "wifi": {"ssid": "Private Home"},
            "recording": {"file": "/tmp/test.mp4"},
            "self_test": {
                "checks": [{"name": "network", "ssid": "Private Home"}]
            },
        }
        with patch("app.diagnostics.get_health", return_value=health), patch(
            "app.diagnostics.utils.get_api_token", return_value=token
        ), patch.dict(
            diagnostics.LOG_FILES, {"ffmpeg.log": lambda: log}, clear=True
        ), patch("app.diagnostics.SERVICE_UNITS", ()), patch(
            "app.diagnostics._command", return_value={"ok": True, "stdout": "", "stderr": ""}
        ):
            data, filename = diagnostics.build_diagnostic_bundle()

        self.assertTrue(filename.endswith(".zip"))
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            combined = "\n".join(
                archive.read(name).decode("utf-8") for name in archive.namelist()
            )
        self.assertNotIn(token, combined)
        self.assertNotIn("hunter2", combined)
        self.assertNotIn("Private Home", combined)
        self.assertNotIn("private\n-----END", combined)
        self.assertIn("<redacted>", combined)

    def test_storage_write_test_removes_temporary_file(self):
        with patch("app.diagnostics.utils.VIDEOS_DIR", self.tmp.name):
            result = diagnostics._storage_write_test()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(list(Path(self.tmp.name).glob(".medicam-self-test-*")), [])

    def test_audio_probe_transport_status_does_not_override_test_outcome(self):
        measured = {
            "status": "ok",
            "device": {"id": "plughw:CARD=HD,DEV=0"},
            "signal_detected": True,
            "rms_dbfs": -35.0,
        }
        with patch("app.diagnostics.camera.get_settings", return_value={}), patch(
            "app.diagnostics.audio.measure_audio_level", return_value=measured
        ):
            result = diagnostics._audio_test()

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["rms_dbfs"], -35.0)


if __name__ == "__main__":
    unittest.main()
