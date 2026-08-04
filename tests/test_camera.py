import io
import os
import unittest
from unittest.mock import Mock, patch

from app import camera


class CameraSettingsTests(unittest.TestCase):
    def test_fullhd_is_the_maximum_supported_resolution(self):
        self.assertEqual(
            camera.SUPPORTED_RESOLUTIONS,
            {
                "SD": "640x360",
                "HD": "1280x720",
                "FHD": "1920x1080",
            },
        )

    def test_removed_resolution_is_normalized_to_fullhd(self):
        self.assertEqual(
            camera._normalize_settings({"resolution": "3840x2160", "fps": "30"}),
            {
                "resolution": "FHD",
                "fps": "30",
                "audio_enabled": True,
                "audio_device": "auto",
            },
        )

    def test_legacy_fullhd_value_is_supported(self):
        self.assertEqual(
            camera._normalize_settings({"resolution": "1920x1080", "fps": 60}),
            {
                "resolution": "FHD",
                "fps": "30",
                "audio_enabled": True,
                "audio_device": "auto",
            },
        )

    def test_legacy_fps_values_are_normalized_to_30_fps(self):
        for fps in ("15", "60"):
            with self.subTest(fps=fps):
                self.assertEqual(
                    camera._normalize_settings({"resolution": "FHD", "fps": fps}),
                    {
                        "resolution": "FHD",
                        "fps": "30",
                        "audio_enabled": True,
                        "audio_device": "auto",
                    },
                )

    def test_audio_settings_are_normalized(self):
        self.assertEqual(
            camera._normalize_settings(
                {
                    "resolution": "FHD",
                    "fps": "30",
                    "audio_enabled": "off",
                    "audio_device": "  plughw:CARD=Mic,DEV=0  ",
                }
            ),
            {
                "resolution": "FHD",
                "fps": "30",
                "audio_enabled": False,
                "audio_device": "plughw:CARD=Mic,DEV=0",
            },
        )


class CameraCommandTests(unittest.TestCase):
    def test_linux_capture_command_streams_camera_mjpeg_with_v4l2_ctl(self):
        command = camera._build_linux_capture_command(
            "1920x1080",
            "30",
            "videos/test.mp4.mjpeg",
            "/dev/v4l/by-id/camera-video-index0",
        )

        self.assertEqual(command[0], "v4l2-ctl")
        self.assertIn("--silent", command)
        self.assertIn("-d", command)
        self.assertIn("/dev/v4l/by-id/camera-video-index0", command)
        self.assertIn("--set-fmt-video=width=1920,height=1080,pixelformat=MJPG", command)
        self.assertIn("--set-parm=30", command)
        self.assertIn("--stream-mmap=8", command)
        self.assertIn("--stream-to=videos/test.mp4.mjpeg", command)

    def test_linux_ffmpeg_command_remuxes_mjpeg_file_without_reencoding(self):
        command = camera._build_linux_command(
            "videos/test.mp4.mjpeg",
            "30",
            "videos/test.mp4",
        )

        self.assertIn("mjpeg", command)
        self.assertEqual(command[command.index("-loglevel") + 1], "warning")
        self.assertIn("-nostats", command)
        self.assertEqual(command[command.index("-i") + 1], "videos/test.mp4.mjpeg")
        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertNotIn("libx264", command)
        self.assertNotIn("mpeg4", command)
        self.assertNotIn("h264_rkmpp", command)
        self.assertNotIn("-thread_queue_size", command)
        self.assertEqual(command[command.index("-framerate") + 1], "30")
        self.assertNotIn("-bsf:v", command)
        self.assertNotIn("+faststart", command)

    def test_linux_ffmpeg_command_adds_synchronized_aac_without_video_reencoding(self):
        command = camera._build_linux_command(
            "videos/test.mp4.mjpeg",
            "30",
            "videos/test.mp4",
            audio_file="videos/test.mp4.wav",
            audio_start_delay=0.125,
        )

        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-b:a") + 1], "128k")
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-itsoffset") + 1], "0.125000")
        self.assertIn("aresample=async=1:first_pts=0", command)
        self.assertIn("-shortest", command)
        self.assertNotIn("-an", command)
        self.assertNotIn("libx264", command)


class CameraLifecycleTests(unittest.TestCase):
    def setUp(self):
        camera.capture_process = None
        camera.audio_process = None
        camera.ffmpeg_process = None
        camera.ffmpeg_log_file = None
        camera.recording_output_file = None
        camera.recording_raw_file = None
        camera.recording_audio_file = None
        camera.recording_audio_device = None
        camera.recording_audio_start_delay = 0.0
        camera.recording_remux_command = None

    def tearDown(self):
        camera.capture_process = None
        camera.audio_process = None
        camera.ffmpeg_process = None
        camera.ffmpeg_log_file = None
        camera.recording_output_file = None
        camera.recording_raw_file = None
        camera.recording_audio_file = None
        camera.recording_audio_device = None
        camera.recording_audio_start_delay = 0.0
        camera.recording_remux_command = None

    @patch("app.camera.utils.get_output_filename", return_value="videos/test.mp4")
    @patch("app.camera._find_linux_camera_device", return_value=None)
    @patch("app.camera.platform.system", return_value="Linux")
    def test_start_fails_cleanly_when_camera_is_missing(
        self,
        _system,
        _find_camera,
        _output_filename,
    ):
        response = camera.start_recording()

        self.assertEqual(response["status"], "error")
        self.assertIn("not available", response["details"])
        self.assertIsNone(camera.ffmpeg_process)

    def test_stop_reaps_a_process_that_already_exited(self):
        process = Mock()
        process.poll.return_value = 1
        process.stdin = io.BytesIO()
        camera.ffmpeg_process = process
        camera.recording_output_file = "videos/interrupted.mp4"

        response = camera.stop_recording()

        self.assertEqual(response["status"], "recording_stopped")
        self.assertEqual(response["returncode"], 1)
        self.assertIn("already exited", response["warning"])
        self.assertIsNone(camera.ffmpeg_process)

    @patch("app.camera.glob.glob")
    @patch("app.camera._is_character_device", return_value=True)
    def test_camera_discovery_prefers_stable_by_id_capture_link(
        self,
        _is_character_device,
        glob_mock,
    ):
        by_id = "/dev/v4l/by-id/usb-camera-video-index0"
        glob_mock.side_effect = [[by_id], ["/dev/video0"]]

        with patch.dict(os.environ, {}, clear=True):
            candidates = camera._camera_candidates()

        self.assertEqual(candidates[0], by_id)


if __name__ == "__main__":
    unittest.main()
