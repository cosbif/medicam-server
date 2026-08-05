import asyncio
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks, HTTPException

from app import routes, utils


PROBE_JSON = json.dumps(
    {
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "mjpeg",
                "width": 1920,
                "height": 1080,
                "r_frame_rate": "30/1",
            }
        ],
        "format": {"duration": "12.5"},
    }
)


class MediaLibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        os.chdir(self.tmp.name)
        Path("videos").mkdir()
        utils._VIDEO_METADATA_CACHE.clear()
        utils._VIDEO_METADATA_IN_PROGRESS.clear()
        utils._VIDEO_INDEX_CACHE = None
        utils._VIDEO_INDEX_CACHE_PATH = None

    def tearDown(self):
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

    @staticmethod
    def _write_fake_thumbnail(command, **_kwargs):
        Path(command[-1]).write_bytes(b"jpeg-preview")
        return subprocess.CompletedProcess(command, 0, "", "")

    def _populate(self, filenames):
        claimed = utils.claim_video_metadata_work(filenames)
        with patch("app.utils.subprocess.check_output", return_value=PROBE_JSON):
            with patch(
                "app.utils.subprocess.run",
                side_effect=self._write_fake_thumbnail,
            ):
                utils.populate_video_metadata(claimed)

    def test_metadata_and_thumbnail_cache_survive_process_cache_reset(self):
        Path("videos/clip.mp4").write_bytes(b"fake-video")
        first_scan = utils.scan_video_library()
        self.assertEqual(first_scan[0]["metadata_status"], "loading")

        self._populate(["clip.mp4"])
        ready = utils.scan_video_library()[0]
        self.assertEqual(ready["metadata_status"], "ready")
        self.assertEqual(ready["resolution"], "1920x1080")
        self.assertEqual(ready["fps"], 30.0)
        self.assertTrue(ready["thumbnail_ready"])
        self.assertTrue(Path(utils.get_video_thumbnail_path("clip.mp4")).is_file())

        utils._VIDEO_INDEX_CACHE = None
        utils._VIDEO_INDEX_CACHE_PATH = None
        utils._VIDEO_METADATA_CACHE.clear()
        with patch(
            "app.utils.subprocess.check_output",
            side_effect=AssertionError("persistent cache should avoid ffprobe"),
        ):
            persisted = utils.scan_video_library(force_reload=True)[0]

        self.assertEqual(persisted["metadata_status"], "ready")
        self.assertEqual(persisted["duration"], 12.5)

    def test_paginated_list_sorts_and_reports_metadata_state(self):
        Path("videos/small.mp4").write_bytes(b"1")
        Path("videos/large.mp4").write_bytes(b"123456789")
        utils.scan_video_library()
        self._populate(["small.mp4", "large.mp4"])

        response = asyncio.run(
            routes.list_videos(
                BackgroundTasks(),
                page=1,
                page_size=1,
                sort="size",
                order="desc",
                refresh=False,
                _ok=True,
            )
        )

        self.assertEqual(response["videos"][0]["filename"], "large.mp4")
        self.assertEqual(response["pagination"]["total"], 2)
        self.assertEqual(response["pagination"]["total_pages"], 2)
        self.assertTrue(response["pagination"]["has_next"])
        self.assertEqual(response["metadata_pending"], 0)

    def test_batch_delete_is_atomic_about_name_validation_and_clears_cache(self):
        Path("videos/first.mp4").write_bytes(b"first")
        Path("videos/second.mp4").write_bytes(b"second")
        utils.scan_video_library()

        with patch(
            "app.routes.camera.get_recording_status",
            return_value={"recording": False, "state": "idle"},
        ):
            with self.assertRaises(HTTPException) as invalid:
                asyncio.run(
                    routes.delete_videos(
                        routes.DeleteVideosRequest(
                            filenames=["first.mp4", "../second.mp4"]
                        ),
                        _ok=True,
                    )
                )
            self.assertEqual(invalid.exception.status_code, 400)
            self.assertTrue(Path("videos/first.mp4").exists())

            result = asyncio.run(
                routes.delete_videos(
                    routes.DeleteVideosRequest(
                        filenames=["first.mp4", "second.mp4"]
                    ),
                    _ok=True,
                )
            )

        self.assertEqual(result["files"], ["first.mp4", "second.mp4"])
        self.assertEqual(utils.scan_video_library(), [])

    def test_library_deletion_is_blocked_while_recording(self):
        Path("videos/clip.mp4").write_bytes(b"video")
        with patch(
            "app.routes.camera.get_recording_status",
            return_value={"recording": True, "state": "recording"},
        ):
            with self.assertRaises(HTTPException) as context:
                asyncio.run(routes.delete_video("clip.mp4", _ok=True))

        self.assertEqual(context.exception.status_code, 409)
        self.assertTrue(Path("videos/clip.mp4").exists())


if __name__ == "__main__":
    unittest.main()
