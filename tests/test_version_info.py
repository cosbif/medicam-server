import unittest
from unittest.mock import patch

from app import version_info


class VersionInfoTests(unittest.TestCase):
    def test_version_payload_contains_product_protocols_and_uptime(self):
        with patch("app.version_info._release_tag", return_value="medicam-v1.2.0"), patch(
            "app.version_info._image_version", return_value="medicam-image-7"
        ), patch(
            "app.version_info._os_release",
            return_value={"PRETTY_NAME": "Test OS"},
        ), patch(
            "app.version_info._camera_firmware",
            return_value={"available": True, "firmware_revision": "0102"},
        ), patch("app.version_info._system_uptime_seconds", return_value=123.4):
            payload = version_info.get_version_info()

        self.assertEqual(payload["server"]["release"], "medicam-v1.2.0")
        self.assertEqual(payload["protocols"]["app"], 4)
        self.assertEqual(payload["protocols"]["ble"], 3)
        self.assertEqual(payload["device_image"]["version"], "medicam-image-7")
        self.assertEqual(payload["camera"]["firmware_revision"], "0102")
        self.assertEqual(payload["uptime_seconds"], 123.4)

    def test_ping_exposes_commit_for_root_healthcheck(self):
        payload = version_info.get_ping_version()

        self.assertIn("commit", payload)
        self.assertEqual(payload["protocol"], version_info.APP_PROTOCOL_VERSION)


if __name__ == "__main__":
    unittest.main()
