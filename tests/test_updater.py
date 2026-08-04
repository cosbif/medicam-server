import subprocess
import unittest
from unittest.mock import patch

from app import updater


class UpdaterTests(unittest.TestCase):
    def test_apply_update_schedules_restart_after_response(self):
        commands = []

        def fake_run(cmd, **kwargs):
            commands.append(cmd)
            stdout = ""
            if cmd == ["git", "rev-parse", "HEAD"]:
                stdout = "abc123"
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=stdout,
                stderr="",
            )

        with patch("app.updater.subprocess.run", side_effect=fake_run):
            with patch("app.updater.time.time", return_value=1234567890):
                result = updater.apply_update()

        self.assertTrue(result["ok"])
        self.assertEqual(result["restart_unit"], "medicam-restart-1234567890")
        self.assertIn(
            [
                "sudo",
                "/usr/bin/systemd-run",
                "--unit",
                "medicam-restart-1234567890",
                "--on-active=1",
                "--collect",
                "/bin/systemctl",
                "restart",
                "medicam.service",
            ],
            commands,
        )
        self.assertNotIn(
            ["sudo", "/bin/systemctl", "start", "restart-medicam.service"],
            commands,
        )

    def test_apply_update_reports_restart_schedule_failure(self):
        def fake_run(cmd, **kwargs):
            if cmd[:2] == ["sudo", "/usr/bin/systemd-run"]:
                return subprocess.CompletedProcess(
                    args=cmd,
                    returncode=1,
                    stdout="",
                    stderr="systemd-run failed",
                )
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="",
                stderr="",
            )

        with patch("app.updater.subprocess.run", side_effect=fake_run):
            result = updater.apply_update()

        self.assertFalse(result["ok"])
        self.assertEqual(result["step"], "restart")
        self.assertEqual(result["returncode"], 1)
        self.assertEqual(result["stderr"], "systemd-run failed")


if __name__ == "__main__":
    unittest.main()
