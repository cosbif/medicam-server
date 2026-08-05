import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from app import updater


PREVIOUS = "a" * 40
TARGET = "b" * 40
TAG = "medicam-v1.2.0"


def command_result(ok=True, stdout="", stderr=""):
    return {
        "ok": ok,
        "stdout": stdout,
        "stderr": stderr,
        "returncode": 0 if ok else 1,
    }


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name) / "repo"
        self.repo.mkdir()
        (self.repo / "medicam-venv/bin").mkdir(parents=True)
        (self.repo / "medicam-venv/bin/pip").touch()
        self.state = Path(self.tmp.name) / "state/status.json"
        self.log = self.state.with_name("update.log")
        self.patchers = [
            patch.object(updater, "REPO_DIR", self.repo),
            patch.object(updater, "STATE_DIR", self.state.parent),
            patch.object(updater, "STATE_FILE", self.state),
            patch.object(updater, "UPDATE_LOG_FILE", self.log),
            patch.object(updater, "ALLOWED_SIGNERS_FILE", self.repo / "allowed"),
            patch.object(updater, "ACTIVATION_REQUEST_FILE", self.state.with_name("request.json")),
        ]
        for patcher in self.patchers:
            patcher.start()

    def tearDown(self):
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.tmp.cleanup()

    def test_fetch_prunes_revoked_stable_tags(self):
        with patch("app.updater._git", return_value=command_result()) as git:
            updater._fetch_release_tags()

        git.assert_called_once_with(
            "fetch",
            "--prune",
            "--force",
            "origin",
            "+refs/tags/medicam-v*:refs/tags/medicam-v*",
            timeout=180,
        )

    def test_persistent_runtime_files_survive_checkout_restore(self):
        settings = self.repo / "camera_settings.json"
        provision = self.repo / "provision.json"
        settings.write_text('{"audio_enabled": true}', encoding="utf-8")
        provision.write_text('{"api_token": "secret"}', encoding="utf-8")
        settings.chmod(0o640)

        snapshot = updater._snapshot_persistent_files()
        settings.write_text("repository default", encoding="utf-8")
        provision.unlink()
        updater._restore_persistent_files(snapshot)

        self.assertEqual(
            settings.read_text(encoding="utf-8"),
            '{"audio_enabled": true}',
        )
        self.assertEqual(
            provision.read_text(encoding="utf-8"),
            '{"api_token": "secret"}',
        )
        self.assertEqual(settings.stat().st_mode & 0o777, 0o640)

    def test_check_ignores_unsigned_tags_and_selects_latest_trusted_release(self):
        tags = {
            "medicam-v1.1.0": "1" * 40,
            "medicam-v1.2.0": "2" * 40,
            "medicam-v9.0.0": "9" * 40,
        }

        def verify(tag):
            return (False, "unknown signer") if tag == "medicam-v9.0.0" else (True, "")

        with patch("app.updater.get_local_commit", return_value=PREVIOUS), patch(
            "app.updater._remote_release_tags", return_value=tags
        ), patch("app.updater._fetch_release_tags"), patch(
            "app.updater._verify_tag", side_effect=verify
        ), patch(
            "app.updater._tag_commit",
            side_effect=lambda tag: {
                "medicam-v1.1.0": "1" * 40,
                "medicam-v1.2.0": TARGET,
            }.get(tag),
        ), patch("app.updater._current_signed_release", return_value=None), patch(
            "app.updater._is_ancestor", return_value=False
        ):
            result = updater.check_for_update()

        self.assertTrue(result["ok"])
        self.assertEqual(result["latest_release"]["tag"], TAG)
        self.assertEqual(result["latest_release"]["commit"], TARGET)
        self.assertTrue(result["latest_release"]["signature_verified"])
        self.assertTrue(result["update_available"])
        self.assertEqual(result["untrusted_tags"][0]["tag"], "medicam-v9.0.0")

    def test_check_never_downgrades_a_commit_ahead_of_latest_release(self):
        with patch("app.updater.get_local_commit", return_value=PREVIOUS), patch(
            "app.updater._remote_release_tags", return_value={TAG: "2" * 40}
        ), patch("app.updater._fetch_release_tags"), patch(
            "app.updater._verify_tag", return_value=(True, "")
        ), patch("app.updater._tag_commit", return_value=TARGET), patch(
            "app.updater._current_signed_release", return_value=None
        ), patch("app.updater._is_ancestor", return_value=True):
            result = updater.check_for_update()

        self.assertFalse(result["update_available"])

    def test_check_never_offers_lower_semver_from_unrelated_history(self):
        current_release = {
            "tag": "medicam-v2.0.0",
            "version": "2.0.0",
            "commit": PREVIOUS,
            "signature_verified": True,
        }
        with patch("app.updater.get_local_commit", return_value=PREVIOUS), patch(
            "app.updater._remote_release_tags", return_value={TAG: "2" * 40}
        ), patch("app.updater._fetch_release_tags"), patch(
            "app.updater._verify_tag", return_value=(True, "")
        ), patch("app.updater._tag_commit", return_value=TARGET), patch(
            "app.updater._current_signed_release", return_value=current_release
        ), patch("app.updater._is_ancestor", return_value=False) as ancestor:
            result = updater.check_for_update()

        self.assertFalse(result["update_available"])
        ancestor.assert_not_called()

    def test_run_update_verifies_hash_and_writes_fixed_activation_request(self):
        release = {
            "ok": True,
            "update_available": True,
            "latest_release": {
                "tag": TAG,
                "version": "1.2.0",
                "commit": TARGET,
                "signature_verified": True,
            },
        }
        commands = []

        def run(command, **_kwargs):
            commands.append(command)
            return command_result()

        with patch("app.updater.get_local_commit", return_value=PREVIOUS), patch(
            "app.updater.check_for_update", return_value=release
        ), patch("app.updater._verify_tag", return_value=(True, "")), patch(
            "app.updater._tag_commit", return_value=TARGET
        ), patch("app.updater._git", return_value=command_result()), patch(
            "app.updater._run", side_effect=run
        ):
            result = updater.run_update("job-success")

        self.assertEqual(result["state"], "restarting")
        request = json.loads(updater.ACTIVATION_REQUEST_FILE.read_text())
        self.assertEqual(request["previous_commit"], PREVIOUS)
        self.assertEqual(request["target_commit"], TARGET)
        self.assertEqual(request["target_tag"], TAG)
        self.assertEqual(updater.ACTIVATION_REQUEST_FILE.stat().st_mode & 0o777, 0o600)
        self.assertFalse(any(command and command[0] == "sudo" for command in commands))
        self.assertNotIn(["git", "reset", "--hard", "origin/main"], commands)

    def test_signature_commit_mismatch_fails_before_checkout(self):
        release = {
            "ok": True,
            "update_available": True,
            "latest_release": {"tag": TAG, "commit": TARGET},
        }
        git_mock = Mock(return_value=command_result())
        with patch("app.updater.get_local_commit", return_value=PREVIOUS), patch(
            "app.updater.check_for_update", return_value=release
        ), patch("app.updater._verify_tag", return_value=(True, "")), patch(
            "app.updater._tag_commit", return_value="c" * 40
        ), patch("app.updater._git", git_mock):
            result = updater.run_update("job-mismatch")

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "release_signature_invalid")
        self.assertNotIn(call("reset", "--hard", TARGET, timeout=60), git_mock.call_args_list)

    def test_dependency_failure_restores_previous_checkout(self):
        release = {
            "ok": True,
            "update_available": True,
            "latest_release": {"tag": TAG, "commit": TARGET},
        }
        git_commands = []
        run_calls = 0

        def git(*args, **_kwargs):
            git_commands.append(args)
            return command_result()

        def run(_command, **_kwargs):
            nonlocal run_calls
            run_calls += 1
            return command_result(ok=run_calls != 1, stderr="pip failed")

        with patch(
            "app.updater.get_local_commit", side_effect=[PREVIOUS, TARGET]
        ), patch("app.updater.check_for_update", return_value=release), patch(
            "app.updater._verify_tag", return_value=(True, "")
        ), patch("app.updater._tag_commit", return_value=TARGET), patch(
            "app.updater._git", side_effect=git
        ), patch("app.updater._run", side_effect=run):
            result = updater.run_update("job-pip-failure")

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result["error_code"], "dependency_install_failed")
        self.assertIn(("reset", "--hard", PREVIOUS), git_commands)

    def test_start_update_rejects_concurrent_job(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(
            json.dumps({"state": "installing", "job_id": "active"}),
            encoding="utf-8",
        )

        with self.assertRaises(updater.UpdateBusyError) as context:
            updater.start_update()

        self.assertEqual(context.exception.code, "update_in_progress")

    def test_start_update_clears_previous_rollback_diagnostics(self):
        self.state.parent.mkdir(parents=True)
        self.state.write_text(
            json.dumps(
                {
                    "state": "rolled_back",
                    "failed_commit": TARGET,
                    "failed_tag": TAG,
                    "error": "old failure",
                    "error_code": "updated_backend_unhealthy",
                    "rollback": True,
                }
            ),
            encoding="utf-8",
        )

        with patch("app.updater.get_local_commit", return_value=PREVIOUS), patch(
            "app.updater.threading.Thread"
        ) as thread:
            status = updater.start_update()

        self.assertEqual(status["state"], "queued")
        self.assertIsNone(status["failed_commit"])
        self.assertIsNone(status["failed_tag"])
        self.assertIsNone(status["error"])
        self.assertIsNone(status["error_code"])
        self.assertFalse(status["rollback"])
        thread.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
