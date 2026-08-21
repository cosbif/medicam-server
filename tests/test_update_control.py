from __future__ import annotations

import unittest
from unittest.mock import patch

from app import update_control


class UpdateControlTests(unittest.TestCase):
    def test_active_recording_blocks_update_and_releases_reservation(self):
        with patch(
            "app.update_control.diagnostics.begin_recording_start",
            return_value=True,
        ), patch(
            "app.update_control.camera.get_persisted_recording_status",
            return_value={"capture_active": True, "state": "recording"},
        ), patch(
            "app.update_control.diagnostics.end_recording_start"
        ) as release, patch(
            "app.update_control.updater.start_update"
        ) as start:
            with self.assertRaises(update_control.UpdateStartBlockedError) as context:
                update_control.start_signed_update()

        self.assertEqual(context.exception.code, "recording_in_progress")
        self.assertEqual(context.exception.state, "recording")
        start.assert_not_called()
        release.assert_called_once_with()

    def test_busy_hardware_blocks_update_before_recording_state_read(self):
        with patch(
            "app.update_control.diagnostics.begin_recording_start",
            return_value=False,
        ), patch(
            "app.update_control.camera.get_persisted_recording_status"
        ) as recording, patch(
            "app.update_control.updater.start_update"
        ) as start:
            with self.assertRaises(update_control.UpdateStartBlockedError) as context:
                update_control.start_signed_update()

        self.assertEqual(context.exception.code, "device_busy")
        recording.assert_not_called()
        start.assert_not_called()

    def test_idle_camera_queues_existing_signed_updater(self):
        queued = {"job_id": "a" * 32, "state": "queued"}
        with patch(
            "app.update_control.diagnostics.begin_recording_start",
            return_value=True,
        ), patch(
            "app.update_control.camera.get_persisted_recording_status",
            return_value={"capture_active": False, "state": "idle"},
        ), patch(
            "app.update_control.updater.start_update",
            return_value=queued,
        ) as start, patch(
            "app.update_control.diagnostics.end_recording_start"
        ) as release:
            result = update_control.start_signed_update()

        self.assertEqual(result, queued)
        start.assert_called_once_with()
        release.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
