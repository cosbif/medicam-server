import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, call, patch

from app import utils
from app.bluetooth_provision import ProvisionService
from app.manage_ble import disable_legacy_ble_services, should_run_ble


class NmcliParsingTests(unittest.TestCase):
    def test_split_nmcli_escaped_keeps_colons_inside_ssid(self):
        self.assertEqual(
            utils.split_nmcli_escaped(r"My\:WiFi:82:WPA2"),
            ["My:WiFi", "82", "WPA2"],
        )

    def test_wifi_password_is_not_passed_as_process_argument(self):
        completed = subprocess.CompletedProcess(
            args=["nmcli"],
            returncode=0,
            stdout="Device activated",
            stderr="",
        )

        with patch("app.utils.subprocess.run", return_value=completed) as run:
            with patch("app.utils.get_primary_ipv4", return_value="192.168.1.50"):
                result = utils.connect_wifi_nmcli("Office", "secret-password")

        self.assertTrue(result["ok"])
        args = run.call_args.args[0]
        kwargs = run.call_args.kwargs
        self.assertIn("--ask", args)
        self.assertNotIn("secret-password", args)
        self.assertEqual(kwargs["input"], "secret-password\n")
        self.assertEqual(result["ip"], "192.168.1.50")

    def test_rejects_invalid_wifi_input_before_running_nmcli(self):
        with patch("app.utils.subprocess.run") as run:
            short_password = utils.connect_wifi_nmcli("Office", "short")
            long_ssid = utils.connect_wifi_nmcli("x" * 33, "valid-pass")

        run.assert_not_called()
        self.assertEqual(short_password["error_code"], "invalid_password")
        self.assertEqual(long_ssid["error_code"], "ssid_too_long")

    def test_classifies_common_nmcli_failures(self):
        self.assertEqual(
            utils.classify_nmcli_error(
                "Secrets were required, but not provided: invalid secrets"
            ),
            "invalid_password",
        )
        self.assertEqual(
            utils.classify_nmcli_error("No network with SSID 'Missing' found"),
            "network_not_found",
        )
        self.assertEqual(
            utils.classify_nmcli_error("Activation timed out"),
            "connection_timeout",
        )


class ProvisionFileTests(unittest.TestCase):
    def test_reset_clears_previous_provisioning_info(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision_path = Path(tmp) / "provision.json"

            with patch("app.utils._provision_path", Mock(return_value=provision_path)):
                utils.set_provisioned(True, {"ssid": "Old", "ip": "192.168.1.2"})
                utils.set_provisioned(False, {})

                data = json.loads(provision_path.read_text())

        self.assertFalse(data["provisioned"])
        self.assertNotIn("ssid", data["info"])
        self.assertNotIn("ip", data["info"])
        self.assertNotIn("api_token", data)
        self.assertIn("updated_at", data["info"])

    def test_set_provisioned_replaces_existing_file_atomically(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision_path = Path(tmp) / "provision.json"
            provision_path.write_text(
                json.dumps({
                    "provisioned": True,
                    "info": {"ssid": "Old"},
                })
            )

            with patch("app.utils._provision_path", Mock(return_value=provision_path)):
                utils.set_provisioned(True, {"ssid": "New"})

            data = json.loads(provision_path.read_text())

        self.assertTrue(data["provisioned"])
        self.assertEqual(data["info"]["ssid"], "New")

    def test_set_provisioned_creates_and_verifies_api_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision_path = Path(tmp) / "provision.json"

            with patch("app.utils._provision_path", Mock(return_value=provision_path)):
                utils.set_provisioned(True, {"ssid": "Office"})
                token = utils.get_api_token()

                self.assertTrue(token)
                self.assertTrue(utils.verify_api_token(token))
                self.assertFalse(utils.verify_api_token("wrong"))

    def test_recovery_window_expires_and_successful_provisioning_closes_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision_path = Path(tmp) / "provision.json"

            with patch("app.utils._provision_path", Mock(return_value=provision_path)):
                utils.set_provisioned(True, {"ssid": "Office"})
                expires_at = utils.start_ble_recovery(120)
                before_expiry = datetime.fromisoformat(expires_at) - timedelta(seconds=1)
                after_expiry = datetime.fromisoformat(expires_at) + timedelta(seconds=1)

                self.assertTrue(utils.is_ble_recovery_active(before_expiry))
                self.assertFalse(utils.is_ble_recovery_active(after_expiry))

                utils.set_provisioned(True, {"ssid": "Office"})
                self.assertFalse(utils.is_ble_recovery_active())

    def test_device_identity_is_stable_and_not_raw_machine_id(self):
        with patch.dict("os.environ", {"MEDICAM_DEVICE_ID": "factory-secret"}):
            first = utils.get_device_id()
            second = utils.get_device_id()

        self.assertEqual(first, second)
        self.assertEqual(len(first), 8)
        self.assertNotIn("factory", first.lower())
        self.assertTrue(utils.get_device_name().startswith("Medicam-"))
    def test_get_video_path_rejects_traversal_and_non_video_names(self):
        for filename in (
            "../secret.mp4",
            "nested/file.mp4",
            "clip.mjpeg",
            "a" * 256 + ".mp4",
            "",
        ):
            with self.subTest(filename=filename):
                with self.assertRaises(ValueError):
                    utils.get_video_path(filename)

        self.assertEqual(utils.get_video_path("clip.mp4"), "videos/clip.mp4")

    def test_get_video_metadata_caches_unchanged_file_probe(self):
        with tempfile.NamedTemporaryFile(suffix=".mp4") as video:
            video.write(b"fake")
            video.flush()

            utils._VIDEO_METADATA_CACHE.clear()
            probe_json = json.dumps(
                {
                    "streams": [
                        {
                            "codec_type": "video",
                            "codec_name": "mjpeg",
                            "width": 1920,
                            "height": 1080,
                            "r_frame_rate": "30/1",
                        },
                        {
                            "codec_type": "audio",
                            "codec_name": "aac",
                            "channels": 1,
                            "sample_rate": "48000",
                        },
                    ],
                    "format": {"duration": "10.0"},
                }
            )

            with patch(
                "app.utils.subprocess.check_output",
                return_value=probe_json,
            ) as check_output:
                first = utils.get_video_metadata(video.name)
                second = utils.get_video_metadata(video.name)

            self.assertEqual(first, second)
            self.assertEqual(check_output.call_count, 1)
            self.assertEqual(first["resolution"], "1920x1080")
            self.assertEqual(first["fps"], 30.0)
            self.assertTrue(first["has_audio"])
            self.assertEqual(first["audio_codec"], "aac")
            self.assertEqual(first["audio_channels"], 1)
            self.assertEqual(first["audio_sample_rate"], 48000)


class BleManagerTests(unittest.TestCase):
    def test_ble_runs_for_setup_disconnect_and_recovery_windows(self):
        self.assertTrue(should_run_ble(False, True, False, False))
        self.assertTrue(should_run_ble(True, False, False, False))
        self.assertTrue(should_run_ble(True, True, True, False))
        self.assertTrue(should_run_ble(True, True, False, True))
        self.assertFalse(should_run_ble(True, True, False, False))

    def test_manager_stops_and_disables_legacy_ble_service(self):
        enabled_result = subprocess.CompletedProcess(
            args=["systemctl"],
            returncode=0,
        )
        with patch("app.manage_ble.service_status", return_value="active"):
            with patch(
                "app.manage_ble.subprocess.run",
                return_value=enabled_result,
            ):
                with patch("app.manage_ble.systemctl", return_value=True) as control:
                    disable_legacy_ble_services()

        self.assertEqual(
            control.call_args_list,
            [
                call("stop", "ble-provision.service"),
                call("disable", "ble-provision.service"),
            ],
        )


class BluetoothProvisioningTests(unittest.TestCase):
    def test_on_command_dispatches_complete_messages_outside_write_callback(self):
        service = object.__new__(ProvisionService)
        service._cmd_buffer = bytearray()
        dispatched = []

        service._dispatch_command = dispatched.append
        service._set_response_async = Mock()

        service.on_command(
            list(b'{"cmd":"STATUS","request_id":"req-1"}\n'),
            {},
        )

        self.assertEqual(
            dispatched,
            [{"cmd": "STATUS", "request_id": "req-1"}],
        )
        service._set_response_async.assert_not_called()

    def test_scan_wifi_parses_nmcli_output(self):
        service = object.__new__(ProvisionService)
        completed = subprocess.CompletedProcess(
            args=["nmcli"],
            returncode=0,
            stdout="Home\\:Main:87:WPA2\nCafe:52:\n",
            stderr="",
        )

        with patch("app.bluetooth_provision.subprocess.run", return_value=completed):
            networks = service.scan_wifi()

        self.assertEqual(
            networks,
            [
                {
                    "ssid": "Home:Main",
                    "signal": 87,
                    "secured": True,
                    "security": "WPA2",
                    "supported": True,
                },
                {
                    "ssid": "Cafe",
                    "signal": 52,
                    "secured": False,
                    "security": "",
                    "supported": True,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
