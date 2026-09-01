import json
import hashlib
import hmac
import os
import stat
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import ANY, Mock, call, patch

from app import utils
from app.bluetooth_provision import (
    ProvisionService,
    configure_always_on_adapter,
    encode_notification_frames,
)
from app.manage_ble import (
    disable_legacy_ble_services,
    reconcile_ble_service,
    should_run_ble,
    write_manager_state,
)


class NmcliParsingTests(unittest.TestCase):
    def test_split_nmcli_escaped_keeps_colons_inside_ssid(self):
        self.assertEqual(
            utils.split_nmcli_escaped(r"My\:WiFi:82:WPA2"),
            ["My:WiFi", "82", "WPA2"],
        )

    def test_wifi_status_uses_stable_c_locale(self):
        with patch(
            "app.utils.subprocess.check_output",
            return_value="wifi:connected\nloopback:connected (externally):lo\n",
        ) as check_output:
            self.assertTrue(utils.is_wifi_connected())

        environment = check_output.call_args.kwargs["env"]
        self.assertEqual(environment["LANG"], "C")
        self.assertEqual(environment["LC_ALL"], "C")

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
        with patch("app.utils.subprocess.run") as run, patch(
            "app.utils.is_wifi_connected", return_value=False
        ):
            short_password = utils.connect_wifi_nmcli("Office", "short")
            long_ssid = utils.connect_wifi_nmcli("x" * 33, "valid-pass")

        run.assert_not_called()
        self.assertEqual(short_password["error_code"], "invalid_password")
        self.assertEqual(long_ssid["error_code"], "ssid_too_long")

    def test_existing_wifi_connection_does_not_reconnect_or_require_password(self):
        with patch("app.utils.subprocess.run") as run, patch(
            "app.utils.is_wifi_connected", return_value=True
        ), patch("app.utils.get_wifi_ssid", return_value="Office"), patch(
            "app.utils.get_primary_ipv4", return_value="192.168.1.52"
        ):
            result = utils.connect_wifi_nmcli("Office", "wrong")

        run.assert_not_called()
        self.assertTrue(result["ok"])
        self.assertEqual(result["stdout"], "already_connected")
        self.assertEqual(result["ip"], "192.168.1.52")

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
    def test_root_created_lock_is_shared_with_backend_group(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "MEDICAM_PROVISION_FILE": str(Path(temporary) / "provision.json"),
                "MEDICAM_PROVISION_LOCK_FILE": str(
                    Path(temporary) / ".provision.json.lock"
                ),
            },
        ), patch("app.utils.os.geteuid", return_value=0), patch(
            "app.utils._radxa_ids", return_value=(123, 456)
        ), patch("app.utils.os.fchown") as fchown:
            with utils._provision_lock(exclusive=True):
                pass

        fchown.assert_called_once()
        self.assertEqual(fchown.call_args.args[1:], (0, 456))

    def test_ble_provisioned_marker_is_non_secret_and_symlink_safe(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "target"
            target.write_text("preserve", encoding="utf-8")
            marker = directory / "ble-provisioned.state"
            marker.symlink_to(target)

            with patch("app.utils.BLE_PROVISIONED_STATE_FILE", marker):
                utils._write_ble_provisioned_state(True)
                self.assertTrue(utils.is_ble_provisioned())

            self.assertEqual(target.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(marker.is_symlink())
            self.assertEqual(marker.read_text(encoding="ascii"), "1\n")
            self.assertEqual(stat.S_IMODE(os.stat(marker).st_mode), 0o644)

    def test_invalid_ble_provisioned_marker_falls_back_to_private_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "ble-provisioned.state"
            marker.write_text("invalid\n", encoding="ascii")

            with patch("app.utils.BLE_PROVISIONED_STATE_FILE", marker):
                with patch("app.utils.is_provisioned", return_value=False) as fallback:
                    self.assertFalse(utils.is_ble_provisioned())

            fallback.assert_called_once_with()

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
            mode = provision_path.stat().st_mode & 0o777

        self.assertTrue(data["provisioned"])
        self.assertEqual(data["info"]["ssid"], "New")
        self.assertEqual(mode, 0o640)

    def test_read_tightens_inherited_provision_file_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision_path = Path(tmp) / "provision.json"
            token = utils.generate_api_token()
            provision_path.write_text(
                json.dumps({"provisioned": True, "api_token": token}),
                encoding="utf-8",
            )
            provision_path.chmod(0o664)

            with patch("app.utils._provision_path", Mock(return_value=provision_path)):
                self.assertEqual(utils.get_api_token(), token)

            self.assertEqual(provision_path.stat().st_mode & 0o777, 0o640)

    def test_provision_write_replaces_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision_path = Path(tmp) / "provision.json"
            target = Path(tmp) / "target"
            target.write_text("do-not-touch", encoding="utf-8")
            provision_path.symlink_to(target)

            with patch("app.utils._provision_path", Mock(return_value=provision_path)):
                utils.set_provisioned(True, {"ssid": "Office"})

            self.assertEqual(target.read_text(encoding="utf-8"), "do-not-touch")
            self.assertFalse(provision_path.is_symlink())
            self.assertTrue(stat.S_ISREG(provision_path.stat().st_mode))
            self.assertEqual(provision_path.stat().st_mode & 0o777, 0o640)

    def test_rotate_api_token_is_atomic_and_rejects_stale_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            provision_path = Path(tmp) / "provision.json"
            with patch("app.utils._provision_path", Mock(return_value=provision_path)):
                utils.set_provisioned(True, {"ssid": "Office"})
                old_token = utils.get_api_token()
                new_token = utils.generate_api_token()

                self.assertTrue(utils.rotate_api_token(old_token, new_token))
                self.assertFalse(utils.rotate_api_token(old_token, utils.generate_api_token()))
                self.assertFalse(utils.verify_api_token(old_token))
                self.assertTrue(utils.verify_api_token(new_token))

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
            recovery_state = Path(tmp) / "ble-recovery-until.state"

            with patch(
                "app.utils._provision_path", Mock(return_value=provision_path)
            ), patch.object(utils, "BLE_RECOVERY_STATE_FILE", recovery_state):
                utils.set_provisioned(True, {"ssid": "Office"})
                expires_at = utils.start_ble_recovery(120)
                before_expiry = datetime.fromisoformat(expires_at) - timedelta(seconds=1)
                after_expiry = datetime.fromisoformat(expires_at) + timedelta(seconds=1)

                self.assertEqual(
                    recovery_state.read_text(encoding="ascii").strip(),
                    expires_at,
                )
                self.assertEqual(recovery_state.stat().st_mode & 0o777, 0o644)
                self.assertTrue(utils.is_ble_recovery_active(before_expiry))
                self.assertFalse(utils.is_ble_recovery_active(after_expiry))

                utils.set_provisioned(True, {"ssid": "Office"})
                self.assertFalse(utils.is_ble_recovery_active())
                self.assertEqual(recovery_state.read_text(encoding="ascii"), "\n")

    def test_public_recovery_marker_survives_private_state_read_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "ble-recovery-until.state"
            now = datetime.now(timezone.utc)
            marker.write_text(
                f"{(now + timedelta(minutes=5)).isoformat()}\n",
                encoding="ascii",
            )
            marker.chmod(0o644)

            with patch.object(utils, "BLE_RECOVERY_STATE_FILE", marker), patch(
                "app.utils._read_provision_data", return_value={}
            ):
                self.assertTrue(utils.is_ble_recovery_active(now))

    def test_newer_root_recovery_marker_overrides_expired_private_deadline(self):
        with tempfile.TemporaryDirectory() as temporary:
            marker = Path(temporary) / "ble-recovery-until.state"
            now = datetime.now(timezone.utc)
            public_deadline = (now + timedelta(minutes=10)).isoformat()
            marker.write_text(f"{public_deadline}\n", encoding="ascii")
            marker.chmod(0o644)

            with patch.object(utils, "BLE_RECOVERY_STATE_FILE", marker), patch(
                "app.utils._read_provision_data",
                return_value={
                    "ble_recovery_until": (now - timedelta(minutes=1)).isoformat()
                },
            ):
                self.assertEqual(utils.get_ble_recovery_until(), public_deadline)
                self.assertTrue(utils.is_ble_recovery_active(now))

    def test_recovery_marker_replaces_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            provision_path = directory / "provision.json"
            marker = directory / "ble-recovery-until.state"
            target = directory / "target"
            target.write_text("preserve", encoding="utf-8")
            marker.symlink_to(target)

            with patch(
                "app.utils._provision_path", Mock(return_value=provision_path)
            ), patch.object(utils, "BLE_RECOVERY_STATE_FILE", marker):
                expires_at = utils.start_ble_recovery(120)

            self.assertEqual(target.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(marker.is_symlink())
            self.assertEqual(marker.read_text(encoding="ascii").strip(), expires_at)
            self.assertEqual(marker.stat().st_mode & 0o777, 0o644)

    def test_ble_refresh_signal_replaces_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            request = Path(tmp) / "ble-refresh.request"
            target = Path(tmp) / "target"
            target.write_text("do-not-touch", encoding="utf-8")
            request.symlink_to(target)

            with patch.object(utils, "BLE_REFRESH_REQUEST_FILE", request):
                utils.request_ble_refresh()

            self.assertEqual(target.read_text(encoding="utf-8"), "do-not-touch")
            self.assertFalse(request.is_symlink())
            self.assertEqual(request.stat().st_mode & 0o777, 0o600)

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
    def test_ble_unit_hides_adapter_after_service_stop(self):
        unit = (
            Path(__file__).resolve().parents[1]
            / "deploy"
            / "systemd"
            / "medicam-ble.service"
        ).read_text(encoding="utf-8")

        self.assertIn(
            "ExecStopPost=/usr/bin/bluetoothctl discoverable off",
            unit,
        )
        self.assertIn(
            "ExecStopPost=/usr/bin/bluetoothctl pairable off",
            unit,
        )
        self.assertIn(
            "CapabilityBoundingSet=CAP_CHOWN CAP_NET_ADMIN CAP_NET_RAW",
            unit,
        )
        self.assertIn("Group=radxa", unit)
        self.assertIn("PartOf=bluetooth.service", unit)

    def test_ble_is_always_required_for_wifi_recovery(self):
        self.assertTrue(should_run_ble(False, True, False, False))
        self.assertTrue(should_run_ble(True, False, False, False))
        self.assertTrue(should_run_ble(True, True, True, False))
        self.assertTrue(should_run_ble(True, True, False, True))
        self.assertTrue(should_run_ble(True, True, False, False))

    def test_production_units_do_not_enable_authorization_bypass(self):
        deploy = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
        for name in (
            "medicam.service",
            "medicam-ble.service",
            "medicam-ble-manager.service",
        ):
            with self.subTest(unit=name):
                content = (deploy / name).read_text(encoding="utf-8")
                self.assertNotIn(
                    "Environment=MEDICAM_DEVELOPMENT_OPEN_ACCESS=1",
                    content,
                )
                self.assertIn(
                    "EnvironmentFile=-/etc/medicam/medicam.env",
                    content,
                )
                if name == "medicam.service":
                    self.assertNotIn(
                        "Environment=MEDICAM_REMOTE_VIDEO_ENABLED=1",
                        content,
                    )

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

    def test_active_required_ble_is_not_restarted_by_polling_manager(self):
        with patch("app.manage_ble.systemctl", return_value=True) as control:
            reconcile_ble_service(
                should_run=True,
                status="active",
            )

        control.assert_not_called()

    def test_active_ble_is_never_stopped(self):
        with patch("app.manage_ble.systemctl", return_value=True) as control:
            action = reconcile_ble_service(
                should_run=False,
                status="active",
            )

        control.assert_not_called()
        self.assertEqual(action, "unchanged")

    def test_manager_state_replaces_symlink_without_touching_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            target = directory / "target.json"
            target.write_text("preserve", encoding="utf-8")
            state_file = directory / "state.json"
            state_file.symlink_to(target)

            with patch("app.manage_ble.BLE_MANAGER_STATE_FILE", state_file):
                write_manager_state({"required": False, "action": "stopped"})

            self.assertEqual(target.read_text(encoding="utf-8"), "preserve")
            self.assertFalse(state_file.is_symlink())
            self.assertEqual(
                json.loads(state_file.read_text(encoding="utf-8")),
                {"required": False, "action": "stopped"},
            )
            self.assertEqual(stat.S_IMODE(os.stat(state_file).st_mode), 0o644)


class BluetoothProvisioningTests(unittest.TestCase):
    def test_adapter_is_permanently_discoverable_but_not_pairable(self):
        dongle = type(
            "FakeDongle",
            (),
            {
                "powered": False,
                "discoverable": False,
                "discoverabletimeout": 180,
                "pairable": True,
            },
        )()

        configure_always_on_adapter(dongle)

        self.assertTrue(bool(dongle.powered))
        self.assertTrue(bool(dongle.discoverable))
        self.assertEqual(int(dongle.discoverabletimeout), 0)
        self.assertFalse(bool(dongle.pairable))

    def test_bluezero_091_none_return_keeps_response_characteristic(self):
        class FakeCharacteristic:
            def __init__(self, uuid):
                self.uuid_value = uuid
                self.values = []

            def set_value(self, value):
                self.values.append(value)

        class FakePeripheral:
            def __init__(self, **_kwargs):
                self.characteristics = []
                self.dongle = type(
                    "FakeDongle",
                    (),
                    {
                        "alias": "nom",
                        "powered": False,
                        "discoverable": False,
                        "discoverabletimeout": 180,
                        "pairable": True,
                    },
                )()

            def add_service(self, *_args):
                return None

            def add_characteristic(self, **kwargs):
                self.characteristics.append(FakeCharacteristic(kwargs["uuid"]))
                # This is the pinned Bluezero 0.9.1 behaviour.
                return None

        class FakePeripheralModule:
            Peripheral = FakePeripheral

        with patch("app.bluetooth_provision.peripheral", FakePeripheralModule), patch(
            "app.bluetooth_provision.get_adapter_mac", return_value="AA:BB:CC:DD:EE:FF"
        ), patch(
            "app.bluetooth_provision.utils.get_device_name", return_value="Medicam-TEST01"
        ):
            service = ProvisionService()

        try:
            self.assertEqual(service.periph.dongle.alias, "Medicam-TEST01")
            self.assertTrue(bool(service.periph.dongle.discoverable))
            self.assertEqual(int(service.periph.dongle.discoverabletimeout), 0)
            self.assertFalse(bool(service.periph.dongle.pairable))
            self.assertIs(service.cmd_char, service.periph.characteristics[0])
            self.assertIs(service.resp_char, service.periph.characteristics[1])
            service._notify_value(b'{"status":"ok"}')
            self.assertEqual(
                service.resp_char.values,
                [list(b'{"status":"ok"}')],
            )
        finally:
            service._executor.shutdown(wait=False, cancel_futures=True)
            service._notify_executor.shutdown(wait=False, cancel_futures=True)

    def test_large_ble_response_is_framed_with_bounded_values(self):
        payload = json.dumps({"networks": ["x" * 64] * 20}).encode()
        frames = encode_notification_frames(payload, frame_id="deadbeef")

        self.assertGreater(len(frames), 1)
        self.assertTrue(all(len(frame) <= 150 for frame in frames))
        bodies = [frame.split(b"|", 4)[4] for frame in frames]
        self.assertEqual(b"".join(bodies), payload)
        for sequence, frame in enumerate(frames):
            prefix = f"M5|deadbeef|{sequence}|{len(frames)}|".encode()
            self.assertTrue(frame.startswith(prefix))

    def test_oversize_ble_response_is_replaced_by_small_error(self):
        frames = encode_notification_frames(b"x" * 8193, frame_id="deadbeef")
        self.assertEqual(frames, [b'{"error":"response_too_large"}'])

    @staticmethod
    def _pairing_service():
        service = object.__new__(ProvisionService)
        service._pairing_lock = __import__("threading").Lock()
        service._pairing_nonce = "fresh-nonce"
        service._pairing_nonce_issued = __import__("time").monotonic()
        service._pairing_failures = 0
        service._pairing_blocked_until = 0.0
        service._session_id = ""
        service._session_key = ""
        service._session_expires = 0.0
        service._session_counter = -1
        service._set_response = Mock()
        return service

    def test_pairing_unlock_returns_mutual_proof_and_creates_hmac_session(self):
        service = self._pairing_service()
        with patch("app.bluetooth_provision.utils.get_device_id", return_value="DEVICE01"), patch(
            "app.bluetooth_provision.utils.get_device_name", return_value="Medicam-VICE01"
        ), patch(
            "app.bluetooth_provision.utils.get_tls_fingerprint", return_value="a" * 64
        ), patch(
            "app.bluetooth_provision.utils.verify_pairing_client_proof", return_value=True
        ), patch(
            "app.bluetooth_provision.utils.pairing_session_key", return_value="b" * 64
        ), patch(
            "app.bluetooth_provision.utils.pairing_server_proof", return_value="c" * 64
        ):
            service._unlock_pairing(
                {"nonce": "fresh-nonce", "proof": "d" * 64},
                request_id="unlock-1",
            )

        response = service._set_response.call_args.args[0]
        self.assertEqual(response["status"], "unlocked")
        self.assertEqual(response["auth_method"], "physical_code")
        self.assertEqual(response["server_proof"], "c" * 64)
        self.assertEqual(response["tls_fingerprint"], "a" * 64)
        self.assertNotIn("api_token", response)
        self.assertTrue(service._session_id)
        self.assertEqual(service._session_key, "b" * 64)

    def test_pairing_unlock_rejects_invalid_proof_and_rotates_nonce(self):
        service = self._pairing_service()
        original_nonce = service._pairing_nonce
        with patch(
            "app.bluetooth_provision.utils.get_device_id", return_value="DEVICE01"
        ), patch(
            "app.bluetooth_provision.utils.get_tls_fingerprint", return_value="a" * 64
        ), patch(
            "app.bluetooth_provision.utils.verify_pairing_client_proof", return_value=False
        ):
            service._unlock_pairing(
                {"nonce": "fresh-nonce", "proof": "0" * 64},
                request_id="unlock-1",
            )

        response = service._set_response.call_args.args[0]
        self.assertEqual(response["error"], "invalid_pairing_proof")
        self.assertNotEqual(response["pairing_nonce"], original_nonce)
        self.assertFalse(service._session_id)

    def test_owner_pairing_proofs_use_existing_token_without_exposing_it(self):
        token = "A" * 43
        with patch("app.utils.get_api_token", return_value=token):
            client_proof = utils.owner_pairing_client_proof(
                "fresh-nonce",
                "DEVICE01",
            )
            self.assertTrue(
                utils.verify_owner_pairing_client_proof(
                    "fresh-nonce",
                    "DEVICE01",
                    client_proof,
                )
            )
            self.assertFalse(
                utils.verify_owner_pairing_client_proof(
                    "different-nonce",
                    "DEVICE01",
                    client_proof,
                )
            )
            self.assertEqual(
                client_proof,
                "9b72d508545c57f042a320cc3d1d3ec371db75d85cbe23734526d599a997dfc7",
            )

    def test_owner_unlock_creates_session_without_returning_owner_token(self):
        service = self._pairing_service()
        with patch(
            "app.bluetooth_provision.utils.is_provisioned", return_value=True
        ), patch(
            "app.bluetooth_provision.utils.get_device_id", return_value="DEVICE01"
        ), patch(
            "app.bluetooth_provision.utils.get_device_name",
            return_value="Medicam-VICE01",
        ), patch(
            "app.bluetooth_provision.utils.get_tls_fingerprint", return_value="a" * 64
        ), patch(
            "app.bluetooth_provision.utils.verify_owner_pairing_client_proof",
            return_value=True,
        ), patch(
            "app.bluetooth_provision.utils.owner_pairing_session_key",
            return_value="b" * 64,
        ), patch(
            "app.bluetooth_provision.utils.owner_pairing_server_proof",
            return_value="c" * 64,
        ):
            service._unlock_owner(
                {"nonce": "fresh-nonce", "proof": "d" * 64},
                request_id="owner-unlock-1",
            )

        response = service._set_response.call_args.args[0]
        self.assertEqual(response["status"], "unlocked")
        self.assertEqual(response["auth_method"], "owner_token")
        self.assertEqual(response["server_proof"], "c" * 64)
        self.assertNotIn("api_token", response)
        self.assertEqual(service._session_key, "b" * 64)

    def test_owner_unlock_is_available_during_normal_connected_operation(self):
        service = self._pairing_service()
        with patch(
            "app.bluetooth_provision.utils.is_provisioned", return_value=True
        ), patch(
            "app.bluetooth_provision.utils.get_device_id", return_value="DEVICE01"
        ), patch(
            "app.bluetooth_provision.utils.get_device_name",
            return_value="Medicam-VICE01",
        ), patch(
            "app.bluetooth_provision.utils.get_tls_fingerprint", return_value="a" * 64
        ), patch(
            "app.bluetooth_provision.utils.verify_owner_pairing_client_proof",
            return_value=True,
        ), patch(
            "app.bluetooth_provision.utils.owner_pairing_session_key",
            return_value="b" * 64,
        ), patch(
            "app.bluetooth_provision.utils.owner_pairing_server_proof",
            return_value="c" * 64,
        ):
            service._unlock_owner(
                {"nonce": "fresh-nonce", "proof": "d" * 64},
                request_id="owner-unlock-1",
            )

        self.assertEqual(service._set_response.call_args.args[0]["status"], "unlocked")

    def test_wifi_commands_require_valid_monotonic_session_hmac(self):
        service = self._pairing_service()
        service._session_id = "session"
        service._session_key = "ab" * 32
        service._session_expires = __import__("time").monotonic() + 60
        command = {
            "cmd": "SCAN_WIFI",
            "request_id": "scan-1",
            "session_id": "session",
            "counter": 1,
        }
        command["auth"] = hmac.new(
            bytes.fromhex(service._session_key),
            service._canonical_session_command(command),
            hashlib.sha256,
        ).hexdigest()

        self.assertTrue(service._verify_session_command(command))
        self.assertFalse(service._verify_session_command(command))

        tampered = {**command, "counter": 2, "auth": "0" * 64}
        self.assertFalse(service._verify_session_command(tampered))

    def test_wifi_commands_without_unlock_are_always_rejected(self):
        service = self._pairing_service()
        service._worker_scan_wifi = Mock()
        service._worker_connect_wifi = Mock()
        service._handle_command(
            {"cmd": "SCAN_WIFI", "request_id": "scan-without-session"}
        )
        service._set_response.assert_called_with(
            {"error": "pairing_required"},
            request_id="scan-without-session",
        )
        service._set_response.reset_mock()

        service._handle_command(
            {
                "cmd": "CONNECT_WIFI",
                "request_id": "connect-without-session",
                "ssid": "Office",
                "password": "secret-password",
            }
        )

        service._set_response.assert_called_with(
            {"error": "pairing_required"},
            request_id="connect-without-session",
        )
        service._worker_scan_wifi.assert_not_called()
        service._worker_connect_wifi.assert_not_called()

    def test_connect_wifi_derives_owner_token_instead_of_receiving_it(self):
        service = self._pairing_service()
        service._session_id = "session"
        service._session_key = "ab" * 32
        service._session_expires = __import__("time").monotonic() + 60
        service._worker_connect_wifi = Mock()
        command = {
            "cmd": "CONNECT_WIFI",
            "request_id": "connect-1",
            "session_id": "session",
            "sealed": "encrypted-wifi-credentials",
            "counter": 1,
        }
        command["auth"] = hmac.new(
            bytes.fromhex(service._session_key),
            service._canonical_session_command(command),
            hashlib.sha256,
        ).hexdigest()

        with patch(
            "app.bluetooth_provision.utils.derive_owner_api_token",
            return_value="D" * 43,
        ) as derive, patch.object(
            service,
            "_decrypt_wifi_credentials",
            return_value=("Office", "secret-password"),
        ):
            service._handle_command(command)

        derive.assert_called_once_with("ab" * 32, "session", ANY)
        service._worker_connect_wifi.assert_called_once_with(
            "Office",
            "secret-password",
            "D" * 43,
            request_id="connect-1",
        )

    def test_plain_ble_credentials_are_rejected(self):
        service = self._pairing_service()
        service._session_id = "session"
        service._session_key = "ab" * 32
        service._session_expires = __import__("time").monotonic() + 60
        service._worker_connect_wifi = Mock()
        command = {
            "cmd": "CONNECT_WIFI",
            "request_id": "connect-1",
            "session_id": "session",
            "counter": 1,
            "ssid": "Office",
            "password": "secret-password",
            "api_token": "D" * 43,
        }
        command["auth"] = hmac.new(
            bytes.fromhex(service._session_key),
            service._canonical_session_command(command),
            hashlib.sha256,
        ).hexdigest()

        service._handle_command(command)

        service._set_response.assert_called_once_with(
            {"error": "invalid_wifi_credentials"},
            request_id="connect-1",
        )
        service._worker_connect_wifi.assert_not_called()

    def test_aes_gcm_wifi_payload_matches_mobile_protocol_vector(self):
        service = self._pairing_service()
        service._session_key = (
            "7539182fd2ab2de792e3910d13b44b25"
            "ad05006aae27d48f90c7465f7d4bc71a"
        )
        payload = {
            "cmd": "CONNECT_WIFI",
            "session_id": "session-1",
            "counter": 1,
            "request_id": "request-1",
            "sealed": (
                "AAECAwQFBgcICQoLzc44SMOlle-qGdQMCDHO-6sPexeyoSqi_uRD95X2"
                "IFtlZmVs1L0uVWXZq7JT4lvMuvIKPsMfMwRhYSDwnzl-"
            ),
        }

        self.assertEqual(
            service._decrypt_wifi_credentials(payload),
            ("Clinic WiFi", "secret-pass"),
        )
        payload["sealed"] = payload["sealed"][:-1] + "A"
        with self.assertRaises(ValueError):
            service._decrypt_wifi_credentials(payload)

    def test_wifi_state_write_failure_returns_stable_error_code(self):
        service = self._pairing_service()
        service.connect_wifi = Mock(
            return_value={"ok": True, "ip": "192.168.1.50"}
        )
        service._clear_session = Mock()
        with patch(
            "app.bluetooth_provision.utils.set_provisioned",
            side_effect=PermissionError("operation not permitted"),
        ):
            service._worker_connect_wifi(
                "Office",
                "",
                api_token="A" * 43,
                request_id="connect-1",
            )

        service._set_response.assert_called_once_with(
            {"error": "provision_state_write_failed"},
            request_id="connect-1",
        )
        service._clear_session.assert_not_called()

    def test_status_advertises_only_protected_pairing_capabilities(self):
        service = self._pairing_service()
        with patch(
            "app.bluetooth_provision.utils.get_device_id", return_value="DEVICE01"
        ), patch(
            "app.bluetooth_provision.utils.get_device_name",
            return_value="Medicam-VICE01",
        ), patch(
            "app.bluetooth_provision.utils.is_provisioned", return_value=True
        ):
            payload = service._status_payload()

        self.assertNotIn("development_open_access", payload)
        self.assertNotIn("development_open_access", payload["capabilities"])
        self.assertNotIn("tls_fingerprint", payload)
        self.assertEqual(payload["pairing_nonce"], "fresh-nonce")
        self.assertIn("mutual_pairing_proof", payload["capabilities"])
        self.assertIn("session_hmac", payload["capabilities"])

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

    def test_on_command_reassembles_att_safe_write_without_response_chunks(self):
        service = object.__new__(ProvisionService)
        service._cmd_buffer = bytearray()
        dispatched = []
        payload = b'{"cmd":"UNLOCK","nonce":"fresh-nonce","proof":"abc"}\n'

        service._dispatch_command = dispatched.append
        service._set_response_async = Mock()
        for offset in range(0, len(payload), 20):
            service.on_command(list(payload[offset:offset + 20]), {})

        self.assertEqual(
            dispatched,
            [{"cmd": "UNLOCK", "nonce": "fresh-nonce", "proof": "abc"}],
        )
        self.assertEqual(service._cmd_buffer, bytearray())
        service._set_response_async.assert_not_called()

    def test_scan_wifi_parses_nmcli_output(self):
        service = object.__new__(ProvisionService)
        completed = subprocess.CompletedProcess(
            args=["nmcli"],
            returncode=0,
            stdout="Home\\:Main:87:WPA2\nCafe:52:\n",
            stderr="",
        )

        with patch(
            "app.bluetooth_provision.subprocess.run", return_value=completed
        ), patch(
            "app.bluetooth_provision.get_wifi_ssid", return_value="Home:Main"
        ):
            networks = service.scan_wifi()

        self.assertEqual(
            networks,
            [
                {
                    "ssid": "Home:Main",
                    "signal": 87,
                    "secured": True,
                    "security": "WPA2",
                    "connected": True,
                    "supported": True,
                },
                {
                    "ssid": "Cafe",
                    "signal": 52,
                    "secured": False,
                    "security": "",
                    "connected": False,
                    "supported": True,
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
