import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from scripts import medicam_ota_activate as activate


PREVIOUS = "a" * 40
TARGET = "b" * 40
TAG = "medicam-v1.2.0"
TARGET_RELEASE = Path("/signed/target")
PREVIOUS_RELEASE = Path("/signed/previous")


class OtaActivatorTests(unittest.TestCase):
    def test_root_path_consumer_validates_and_schedules_fixed_request(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "format": 1,
                        "job_id": "1" * 32,
                        "previous_commit": PREVIOUS,
                        "target_commit": TARGET,
                        "target_tag": TAG,
                        "requested_at": "2026-08-05T00:00:00+00:00",
                    }
                ),
                encoding="utf-8",
            )
            request.chmod(0o600)
            with patch.object(activate, "REQUEST_FILE", request), patch.object(
                activate, "schedule"
            ) as schedule, patch.object(activate, "append_log"):
                activate.consume_activation_request()

        schedule.assert_called_once_with(PREVIOUS, TARGET, TAG)
        self.assertFalse(request.exists())

    def test_root_path_consumer_rejects_group_writable_or_extra_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            request = Path(directory) / "request.json"
            request.write_text(
                json.dumps(
                    {
                        "format": 1,
                        "job_id": "1" * 32,
                        "previous_commit": PREVIOUS,
                        "target_commit": TARGET,
                        "target_tag": TAG,
                        "unexpected": "value",
                    }
                ),
                encoding="utf-8",
            )
            request.chmod(0o660)
            with patch.object(activate, "REQUEST_FILE", request):
                with self.assertRaisesRegex(activate.ActivationError, "unsafe"):
                    activate._read_activation_request()
                request.chmod(0o600)
                with self.assertRaisesRegex(activate.ActivationError, "unexpected"):
                    activate._read_activation_request()

    def test_usb_power_policy_installs_rule_and_protects_attached_camera(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            source = release / "deploy" / "udev" / activate.USB_POWER_RULES_FILE.name
            source.parent.mkdir(parents=True)
            source.write_text("test rule\n", encoding="utf-8")
            destination = root / "rules" / activate.USB_POWER_RULES_FILE.name
            sysfs = root / "sysfs"
            camera = sysfs / "1-1.2"
            (camera / "power").mkdir(parents=True)
            (camera / "idVendor").write_text("eba4\n", encoding="ascii")
            (camera / "idProduct").write_text("6579\n", encoding="ascii")
            (camera / "power" / "control").write_text("auto\n", encoding="ascii")

            with patch.object(activate, "USB_POWER_RULES_FILE", destination), patch.object(
                activate, "USB_SYSFS_DIR", sysfs
            ), patch.object(activate.os, "fchown"), patch.object(activate, "run") as run:
                activate.install_usb_power_policy(release)

            self.assertEqual(destination.read_text(encoding="utf-8"), "test rule\n")
            self.assertEqual(destination.stat().st_mode & 0o777, 0o644)
            self.assertEqual(
                (camera / "power" / "control").read_text(encoding="ascii"),
                "on\n",
            )
            run.assert_called_once_with(
                ["/usr/bin/udevadm", "control", "--reload-rules"]
            )

    def test_root_restore_preserves_content_mode_and_ownership_metadata(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(
            activate, "REPO", Path(directory)
        ):
            settings = Path(directory) / "camera_settings.json"
            settings.write_text('{"fps": "30"}', encoding="utf-8")
            settings.chmod(0o640)
            snapshot = activate.snapshot_persistent_files()
            settings.write_text("default", encoding="utf-8")
            activate.restore_persistent_files(snapshot)

            self.assertEqual(
                settings.read_text(encoding="utf-8"),
                '{"fps": "30"}',
            )
            self.assertEqual(settings.stat().st_mode & 0o777, 0o640)

    def test_known_legacy_sudoers_rules_are_removed_by_policy_matchers(self):
        legacy = [
            "radxa ALL=(ALL) NOPASSWD: ALL",
            "radxa ALL=NOPASSWD: /usr/bin/systemd-run",
            "radxa ALL=NOPASSWD: /bin/systemctl restart medicam.service",
        ]
        for line in legacy:
            self.assertTrue(
                any(pattern.fullmatch(line) for pattern in activate.LEGACY_SUDOERS_PATTERNS),
                line,
            )

    def test_successful_activation_requires_expected_commit_health(self):
        statuses = []
        with patch.object(activate, "verify_release"), patch.object(
            activate, "validate_transition"
        ), patch.object(
            activate, "materialize_release", return_value=TARGET_RELEASE
        ), patch.object(
            activate, "restart_services"
        ), patch.object(
            activate, "install_release_assets"
        ), patch.object(
            activate, "wait_for_health", return_value=(True, "")
        ) as health, patch.object(
            activate, "write_trust_state"
        ), patch.object(
            activate,
            "write_status",
            side_effect=lambda state, progress, message, **fields: statuses.append(state),
        ):
            activate.perform(PREVIOUS, TARGET, TAG)

        health.assert_called_once_with(TARGET)
        self.assertEqual(statuses[-1], "complete")

    def test_failed_healthcheck_restores_previous_commit_and_records_reason(self):
        statuses = []
        with patch.object(activate, "verify_release"), patch.object(
            activate, "validate_transition"
        ), patch.object(
            activate,
            "materialize_release",
            side_effect=[TARGET_RELEASE, PREVIOUS_RELEASE],
        ), patch.object(
            activate, "restart_services"
        ), patch.object(
            activate,
            "wait_for_health",
            side_effect=[(False, "connection refused"), (True, "")],
        ) as health, patch.object(
            activate, "collect_failure_reason", return_value="backend import failed"
        ), patch.object(activate, "reset_checkout") as reset, patch.object(
            activate, "install_python_requirements"
        ), patch.object(activate, "install_release_assets"), patch.object(
            activate,
            "write_status",
            side_effect=lambda state, progress, message, **fields: statuses.append(
                (state, fields)
            ),
        ):
            activate.perform(PREVIOUS, TARGET, TAG)

        self.assertEqual(health.call_args_list, [call(TARGET), call(PREVIOUS)])
        reset.assert_called_once_with(PREVIOUS)
        self.assertEqual(statuses[-1][0], "rolled_back")
        self.assertTrue(statuses[-1][1]["rollback"])
        self.assertEqual(statuses[-1][1]["failed_commit"], TARGET)
        self.assertEqual(statuses[-1][1]["error"], "backend import failed")

    def test_schedule_reverifies_release_and_uses_root_transient_unit(self):
        with patch.object(activate, "verify_release") as verify, patch.object(
            activate, "validate_transition"
        ), patch.object(
            activate, "materialize_release", return_value=TARGET_RELEASE
        ), patch.object(
            activate, "install_release_assets"
        ), patch.object(activate, "write_status"), patch.object(
            activate.time, "time", return_value=1234
        ), patch.object(activate, "run") as run:
            activate.schedule(PREVIOUS, TARGET, TAG)

        verify.assert_called_once_with(TAG, TARGET)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn("medicam-ota-bbbbbbbbbbbb-1234", command)
        self.assertEqual(command[-4:], ["perform", PREVIOUS, TARGET, TAG])

    def test_schedule_failure_restores_checkout_dependencies_and_assets(self):
        with patch.object(activate, "verify_release"), patch.object(
            activate, "validate_transition"
        ), patch.object(
            activate,
            "materialize_release",
            side_effect=[TARGET_RELEASE, PREVIOUS_RELEASE],
        ), patch.object(
            activate,
            "install_release_assets",
            side_effect=[activate.ActivationError("bad unit"), None],
        ) as assets, patch.object(
            activate, "reset_checkout"
        ) as reset, patch.object(
            activate, "install_python_requirements"
        ) as requirements:
            with self.assertRaisesRegex(
                activate.ActivationError,
                "previous release restored",
            ):
                activate.schedule(PREVIOUS, TARGET, TAG)

        reset.assert_called_once_with(PREVIOUS)
        requirements.assert_called_once_with(PREVIOUS_RELEASE)
        self.assertEqual(
            assets.call_args_list,
            [
                call(TARGET_RELEASE, harden=True),
                call(PREVIOUS_RELEASE, harden=True),
            ],
        )

    def test_hardening_initializes_trust_before_restricting_sudoers(self):
        order = []
        with patch.object(
            activate,
            "install_release_assets",
            side_effect=lambda release, **kwargs: order.append(
                ("assets", release, kwargs["harden"])
            ),
        ), patch.object(
            activate,
            "initialize_trust_state",
            side_effect=lambda: (order.append(("trust", True)) or (PREVIOUS, TAG)),
        ), patch.object(
            activate,
            "materialize_release",
            side_effect=lambda commit: (
                order.append(("materialize", commit)) or PREVIOUS_RELEASE
            ),
        ), patch.object(
            activate,
            "harden_sudoers",
            side_effect=lambda: order.append(("sudoers", True)),
        ):
            result = activate.main(["medicam-ota-activate", "harden"])

        self.assertEqual(result, 0)
        self.assertEqual(
            order,
            [
                ("trust", True),
                ("materialize", PREVIOUS),
                ("assets", PREVIOUS_RELEASE, False),
                ("sudoers", True),
            ],
        )

    def test_restart_failure_also_triggers_rollback(self):
        statuses = []
        with patch.object(activate, "verify_release"), patch.object(
            activate, "validate_transition"
        ), patch.object(
            activate,
            "materialize_release",
            side_effect=[TARGET_RELEASE, PREVIOUS_RELEASE],
        ), patch.object(
            activate,
            "restart_services",
            side_effect=[activate.ActivationError("unit failed"), None],
        ), patch.object(
            activate, "collect_failure_reason", return_value="unit failed"
        ), patch.object(activate, "reset_checkout") as reset, patch.object(
            activate, "install_python_requirements"
        ), patch.object(activate, "install_release_assets"), patch.object(
            activate, "wait_for_health", return_value=(True, "")
        ), patch.object(
            activate,
            "write_status",
            side_effect=lambda state, progress, message, **fields: statuses.append(state),
        ):
            activate.perform(PREVIOUS, TARGET, TAG)

        reset.assert_called_once_with(PREVIOUS)
        self.assertEqual(statuses[-1], "rolled_back")

    def test_transition_rejects_untrusted_previous_commit_and_downgrade(self):
        with patch.object(
            activate,
            "_read_trust_file",
            side_effect=[PREVIOUS, "1.2.0"],
        ):
            with self.assertRaises(activate.ActivationError):
                activate.validate_transition("c" * 40, TAG)

        with patch.object(
            activate,
            "_read_trust_file",
            side_effect=[PREVIOUS, "2.0.0"],
        ):
            with self.assertRaises(activate.ActivationError):
                activate.validate_transition(PREVIOUS, TAG)


if __name__ == "__main__":
    unittest.main()
