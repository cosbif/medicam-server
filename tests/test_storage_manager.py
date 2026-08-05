import json
import os
import tempfile
import unittest
from collections import namedtuple
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from app import storage_manager, utils


DiskUsage = namedtuple("DiskUsage", "total used free")


class StorageManagerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        self.old_policy_file = storage_manager.POLICY_FILE
        os.chdir(self.tmp.name)
        Path("videos").mkdir()
        storage_manager.POLICY_FILE = "storage_policy.json"
        utils._VIDEO_METADATA_CACHE.clear()
        utils._VIDEO_METADATA_IN_PROGRESS.clear()
        utils._VIDEO_INDEX_CACHE = None
        utils._VIDEO_INDEX_CACHE_PATH = None

    def tearDown(self):
        storage_manager.POLICY_FILE = self.old_policy_file
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    def _video(self, name: str, size: int, age_days: int = 0) -> Path:
        path = Path("videos") / name
        with open(path, "wb") as output:
            output.truncate(size)
        timestamp = (datetime.now(timezone.utc) - timedelta(days=age_days)).timestamp()
        os.utime(path, (timestamp, timestamp))
        return path

    def _dynamic_disk_usage(self, initial_free: int, initial_library: int):
        total = 64 * storage_manager.GIB

        def usage():
            current_library = sum(
                path.stat().st_size for path in Path("videos").glob("*.mp4")
            )
            free = initial_free + max(0, initial_library - current_library)
            return DiskUsage(total=total, used=total - free, free=free)

        return usage

    def test_policy_is_atomic_and_invalid_file_fails_safe(self):
        saved = storage_manager.update_policy("keep_last_days", 30)
        self.assertEqual(saved, {"mode": "keep_last_days", "value": 30})
        self.assertEqual(storage_manager.get_policy(), saved)
        self.assertFalse(Path("storage_policy.json.tmp").exists())

        Path("storage_policy.json").write_text("not-json", encoding="utf-8")
        self.assertEqual(storage_manager.get_policy(), {"mode": "off", "value": None})

        with self.assertRaises(storage_manager.StoragePolicyError):
            storage_manager.update_policy("keep_last_gb", 0.5)

    def test_storage_info_uses_measured_rate_and_accounts_for_finalization(self):
        videos = [
            {
                "filename": "measured.mp4",
                "size_bytes": 600 * storage_manager.MIB,
                "duration": 60.0,
                "metadata_status": "ready",
                "created_at": "2026-08-05T10:00:00+00:00",
            }
        ]
        free = 11 * storage_manager.GIB
        disk = DiskUsage(64 * storage_manager.GIB, 53 * storage_manager.GIB, free)

        with patch("app.storage_manager._disk_usage", return_value=disk), patch(
            "app.storage_manager._library_snapshot", return_value=videos
        ):
            info = storage_manager.get_storage_info()

        expected_rate = 10 * storage_manager.MIB * 1.1
        expected_minutes = round(
            (free - storage_manager.CRITICAL_FREE_BYTES)
            / (expected_rate * storage_manager.FINALIZATION_SPACE_MULTIPLIER)
            / 60,
            1,
        )
        self.assertEqual(info["estimate_source"], "measured")
        self.assertEqual(info["estimate_sample_count"], 1)
        self.assertEqual(info["estimated_recording_minutes"], expected_minutes)
        self.assertEqual(info["library"]["largest_files"][0]["filename"], "measured.mp4")

    def test_low_space_policy_deletes_oldest_until_warning_threshold(self):
        old = self._video("old.mp4", 2 * storage_manager.GIB, age_days=5)
        new = self._video("new.mp4", 2 * storage_manager.GIB, age_days=1)
        initial_library = old.stat().st_size + new.stat().st_size
        storage_manager.update_policy("low_space")

        with patch(
            "app.storage_manager._disk_usage",
            side_effect=self._dynamic_disk_usage(
                initial_free=2 * storage_manager.GIB,
                initial_library=initial_library,
            ),
        ):
            result = storage_manager.apply_policy(trigger="recording_start")

        self.assertEqual(result["deleted"], ["old.mp4", "new.mp4"])
        self.assertGreaterEqual(
            result["free_after_bytes"], storage_manager.WARNING_FREE_BYTES
        )
        self.assertEqual(utils.scan_video_library(), [])

    def test_size_policy_keeps_newest_files_within_limit(self):
        old = self._video("old.mp4", 700 * storage_manager.MIB, age_days=5)
        middle = self._video("middle.mp4", 700 * storage_manager.MIB, age_days=3)
        newest = self._video("newest.mp4", 700 * storage_manager.MIB, age_days=1)
        initial_library = old.stat().st_size + middle.stat().st_size + newest.stat().st_size
        storage_manager.update_policy("keep_last_gb", 1)

        with patch(
            "app.storage_manager._disk_usage",
            side_effect=self._dynamic_disk_usage(
                initial_free=20 * storage_manager.GIB,
                initial_library=initial_library,
            ),
        ):
            result = storage_manager.apply_policy(trigger="recording_start")

        self.assertEqual(result["deleted"], ["old.mp4", "middle.mp4"])
        self.assertTrue(Path("videos/newest.mp4").exists())

    def test_age_policy_and_protected_file_are_respected(self):
        old = self._video("old.mp4", 10 * storage_manager.MIB, age_days=40)
        protected = self._video("protected.mp4", 10 * storage_manager.MIB, age_days=40)
        recent = self._video("recent.mp4", 10 * storage_manager.MIB, age_days=2)
        initial_library = old.stat().st_size + protected.stat().st_size + recent.stat().st_size
        storage_manager.update_policy("keep_last_days", 30)

        with patch(
            "app.storage_manager._disk_usage",
            side_effect=self._dynamic_disk_usage(
                initial_free=20 * storage_manager.GIB,
                initial_library=initial_library,
            ),
        ):
            result = storage_manager.apply_policy(
                trigger="recording_stopped",
                protected_filenames={"protected.mp4"},
            )

        self.assertEqual(result["deleted"], ["old.mp4"])
        self.assertTrue(Path("videos/protected.mp4").exists())
        self.assertTrue(Path("videos/recent.mp4").exists())

    def test_manual_cleanup_reclaims_requested_amount_oldest_first(self):
        first = self._video("first.mp4", 300 * storage_manager.MIB, age_days=3)
        second = self._video("second.mp4", 300 * storage_manager.MIB, age_days=2)
        third = self._video("third.mp4", 300 * storage_manager.MIB, age_days=1)
        initial_library = first.stat().st_size + second.stat().st_size + third.stat().st_size

        with patch(
            "app.storage_manager._disk_usage",
            side_effect=self._dynamic_disk_usage(
                initial_free=20 * storage_manager.GIB,
                initial_library=initial_library,
            ),
        ):
            result = storage_manager.reclaim_space(500 * storage_manager.MIB)

        self.assertEqual(result["deleted"], ["first.mp4", "second.mp4"])
        self.assertGreaterEqual(result["reclaimed_bytes"], 500 * storage_manager.MIB)
        self.assertTrue(Path("videos/third.mp4").exists())


if __name__ == "__main__":
    unittest.main()
