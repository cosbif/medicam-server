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
        self.assertIn("updated_at", data["info"])


class BluetoothProvisioningTests(unittest.TestCase):
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
