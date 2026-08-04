import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from app import utils
from app.bluetooth_provision import ProvisionService


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

    def test_get_video_path_rejects_traversal_and_non_video_names(self):
        for filename in ("../secret.mp4", "nested/file.mp4", "clip.mjpeg", ""):
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
                            "width": 1920,
                            "height": 1080,
                            "r_frame_rate": "30/1",
                        }
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
                {"ssid": "Home:Main", "signal": 87, "secured": True},
                {"ssid": "Cafe", "signal": 52, "secured": False},
            ],
        )


if __name__ == "__main__":
    unittest.main()
