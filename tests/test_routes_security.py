import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi import HTTPException
from starlette.requests import Request

from app import routes
from app import utils
from app.routes import AUTH_HEADER


class RouteSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)

        self.provision_path = Path(self.tmp.name) / "provision.json"
        self.videos_dir = Path(self.tmp.name) / "videos"
        self.videos_dir.mkdir()

        self.patchers = [
            patch("app.utils._provision_path", Mock(return_value=self.provision_path)),
            patch("app.utils.is_wifi_connected", Mock(return_value=True)),
            patch("app.utils.get_wifi_ssid", Mock(return_value="Office")),
            patch("app.utils.get_primary_ipv4", Mock(return_value="192.168.1.50")),
            patch(
                "app.routes._systemctl_status",
                Mock(return_value={"ok": True, "status": "inactive"}),
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _provision(self):
        utils.set_provisioned(True, {"ssid": "Office", "ip": "192.168.1.50"})
        return utils.get_api_token()

    @staticmethod
    def _headers(token):
        return {AUTH_HEADER: token}

    @staticmethod
    def _request(headers=None, client_host="192.168.1.20"):
        raw_headers = []
        for key, value in (headers or {}).items():
            raw_headers.append((key.lower().encode(), str(value).encode()))
        return Request(
            {
                "type": "http",
                "headers": raw_headers,
                "client": (client_host, 12345),
            }
        )

    def test_ping_stays_public_for_discovery(self):
        response = asyncio.run(routes.ping())

        self.assertEqual(response["status"], "ok")

    def test_critical_routes_are_registered_with_auth_dependency(self):
        api_token_paths = {
            "/start",
            "/stop",
            "/recording/status",
            "/videos",
            "/videos/{filename}",
            "/download/{filename}",
            "/delete/{filename}",
            "/videos/clear",
            "/storage",
            "/settings",
            "/audio/devices",
            "/audio/test",
            "/wifi",
            "/wifi/connect",
            "/wifi/status",
            "/provision/recovery/start",
            "/provision/recovery/stop",
        }
        update_paths = {
            "/update/check",
            "/update/apply",
        }

        route_by_path = {route.path: route for route in routes.router.routes}
        for path in api_token_paths:
            with self.subTest(path=path):
                route = route_by_path[path]
                dependency_names = {
                    getattr(dependency.call, "__name__", "")
                    for dependency in route.dependant.dependencies
                }

            self.assertIn("require_api_auth", dependency_names)

        for path in update_paths:
            with self.subTest(path=path):
                route = route_by_path[path]
                dependency_names = {
                    getattr(dependency.call, "__name__", "")
                    for dependency in route.dependant.dependencies
                }

            self.assertIn("require_update_auth", dependency_names)

    def test_auth_dependency_rejects_unprovisioned_devices(self):
        with self.assertRaises(HTTPException) as context:
            routes.require_api_auth(self._request())

        self.assertEqual(context.exception.status_code, 403)
        self.assertEqual(context.exception.detail, "device_not_provisioned")

    def test_auth_dependency_requires_valid_api_token_after_provisioning(self):
        token = self._provision()

        for request in (
            self._request(),
            self._request(self._headers("wrong")),
        ):
            with self.subTest(headers=request.headers):
                with self.assertRaises(HTTPException) as context:
                    routes.require_api_auth(request)

            self.assertEqual(context.exception.status_code, 401)
            self.assertEqual(context.exception.detail, "invalid_api_token")

        self.assertTrue(routes.require_api_auth(self._request(self._headers(token))))

    def test_update_auth_allows_loopback_but_rejects_lan_without_token(self):
        with self.assertRaises(HTTPException) as context:
            routes.require_update_auth(self._request())

        self.assertEqual(context.exception.status_code, 403)

        self.assertTrue(routes.require_update_auth(self._request(client_host="127.0.0.1")))

        token = self._provision()
        self.assertTrue(routes.require_update_auth(self._request(self._headers(token))))

    def test_update_is_rejected_while_recording(self):
        with patch(
            "app.routes.camera.get_recording_status",
            return_value={"recording": True, "state": "recording"},
        ):
            with patch("app.routes.updater.apply_update") as apply_mock:
                with self.assertRaises(HTTPException) as context:
                    asyncio.run(routes.update_apply(_ok=True))

        self.assertEqual(context.exception.status_code, 409)
        self.assertEqual(
            context.exception.detail["code"],
            "recording_in_progress",
        )
        apply_mock.assert_not_called()

    def test_update_can_repair_an_interrupted_recording(self):
        with patch(
            "app.routes.camera.get_recording_status",
            return_value={
                "recording": True,
                "capture_active": False,
                "state": "interrupted",
            },
        ):
            with patch(
                "app.routes.updater.apply_update",
                return_value={"ok": True, "step": "done"},
            ) as apply_mock:
                response = asyncio.run(routes.update_apply(_ok=True))

        self.assertTrue(response["ok"])
        apply_mock.assert_called_once_with()

    def test_provision_status_redacts_private_fields_without_token(self):
        token = self._provision()

        public_status = asyncio.run(routes.provision_status(self._request()))
        authenticated_status = asyncio.run(
            routes.provision_status(self._request(self._headers(token)))
        )

        self.assertTrue(public_status["provisioned"])
        self.assertNotIn("info", public_status)
        self.assertNotIn("ssid", public_status["wifi"])
        self.assertNotIn("ip", public_status["wifi"])
        self.assertNotIn("ble_service", public_status)
        self.assertIn("device", public_status)
        self.assertNotIn("recovery", public_status)

        self.assertEqual(authenticated_status["info"]["ssid"], "Office")
        self.assertEqual(authenticated_status["wifi"]["ssid"], "Office")
        self.assertEqual(authenticated_status["wifi"]["ip"], "192.168.1.50")
        self.assertIn("ble_service", authenticated_status)
        self.assertIn("recovery", authenticated_status)

    def test_authenticated_owner_can_open_recovery_window(self):
        token = self._provision()

        with patch(
            "app.routes._systemctl_action",
            return_value={"ok": True, "stdout": "", "stderr": ""},
        ):
            result = asyncio.run(routes.provision_recovery_start(120, _ok=True))

        self.assertEqual(result["status"], "recovery_started")
        self.assertTrue(utils.is_ble_recovery_active())

        stopped = asyncio.run(routes.provision_recovery_stop(_ok=True))
        self.assertEqual(stopped["status"], "recovery_stopped")
        self.assertFalse(utils.is_ble_recovery_active())

        self.assertTrue(utils.verify_api_token(token))

    def test_owner_reset_revokes_token_and_reopens_ble(self):
        token = self._provision()

        with patch(
            "app.routes._systemctl_action",
            return_value={"ok": True, "stdout": "", "stderr": ""},
        ) as systemctl_action:
            result = asyncio.run(
                routes.provision_reset(self._request(self._headers(token)))
            )

        self.assertEqual(result["status"], "reset")
        self.assertFalse(utils.is_provisioned())
        self.assertFalse(utils.verify_api_token(token))
        systemctl_action.assert_called_once_with("restart", routes.BLE_SERVICE)

    def test_video_endpoints_reject_bad_names_and_authorize_streaming(self):
        token = self._provision()
        video = self.videos_dir / "clip.mp4"
        video.write_bytes(b"0123456789")

        with self.assertRaises(HTTPException) as missing_auth:
            routes.require_api_auth(self._request())
        self.assertEqual(missing_auth.exception.status_code, 401)

        with self.assertRaises(HTTPException) as bad_name:
            asyncio.run(
                routes.get_video(
                    "clip.mjpeg",
                    self._request(self._headers(token)),
                    _ok=True,
                )
            )
        self.assertEqual(bad_name.exception.status_code, 400)

        with self.assertRaises(HTTPException) as invalid_range:
            asyncio.run(
                routes.get_video(
                    "clip.mp4",
                    self._request({**self._headers(token), "Range": "bytes=99-120"}),
                    _ok=True,
                )
            )
        self.assertEqual(invalid_range.exception.status_code, 416)

        partial = asyncio.run(
            routes.get_video(
                "clip.mp4",
                self._request({**self._headers(token), "Range": "bytes=2-5"}),
                _ok=True,
            )
        )
        self.assertEqual(partial.status_code, 206)
        self.assertEqual(partial.body, b"2345")


if __name__ == "__main__":
    unittest.main()
