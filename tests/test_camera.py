import io
import json
import os
import tempfile
import time
import unittest
from unittest.mock import Mock, mock_open, patch

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
    @patch("app.camera.subprocess.run")
    def test_recording_probe_uses_fast_mp4_frame_count(self, run_mock):
        run_mock.return_value = Mock(
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {
                            "nb_frames": "1800",
                            "avg_frame_rate": "30/1",
                            "width": 1920,
                            "height": 1080,
                        }
                    ],
                    "format": {"duration": "60.0"},
                }
            ),
            stderr="",
        )

        quality = camera._probe_recording("videos/test.mp4", 60.0, 30.0)

        self.assertTrue(quality["healthy"])
        self.assertEqual(quality["frame_count"], 1800)
        self.assertEqual(run_mock.call_count, 1)
        self.assertNotIn("-count_frames", run_mock.call_args.args[0])

    def test_long_recordings_receive_size_based_processing_timeout(self):
        with patch("app.camera._safe_file_size", return_value=8 * 1024 ** 3):
            timeout = camera._file_processing_timeout(
                "videos/long.mjpeg",
                camera.FFMPEG_REMUX_TIMEOUT,
                camera.REMUX_MIN_THROUGHPUT_BYTES_PER_SECOND,
            )

        self.assertGreater(timeout, 30 * 60)

    def test_mjpeg_frame_counter_uses_jpeg_start_markers(self):
        with tempfile.NamedTemporaryFile() as raw:
            raw.write(b"\xff\xd8frame-a\xff\xd9\xff\xd8frame-b\xff\xd9")
            raw.flush()

            self.assertEqual(camera._count_mjpeg_frames(raw.name), 2)

    def test_audio_temp_file_uses_memory_backed_storage(self):
        with patch.object(camera, "AUDIO_TEMP_DIR", "/run/medicam"):
            with patch("app.camera.os.path.isdir", return_value=True):
                with patch("app.camera.os.access", return_value=True):
                    path = camera._build_audio_temp_file(
                        "videos/12-00-00_01.01.2026.mp4"
                    )

        self.assertEqual(
            path,
            "/run/medicam/medicam-12-00-00_01.01.2026.mp4.pcm",
        )

    def test_linux_capture_command_streams_camera_mjpeg_with_ffmpeg(self):
        command = camera._build_linux_capture_command(
            "1920x1080",
            "30",
            "videos/test.mp4.mjpeg",
            "/dev/v4l/by-id/camera-video-index0",
        )

        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-thread_queue_size", command)
        self.assertEqual(
            command[command.index("-thread_queue_size") + 1],
            camera.LINUX_INPUT_QUEUE_SIZE,
        )
        self.assertIn("/dev/v4l/by-id/camera-video-index0", command)
        self.assertIn("1920x1080", command)
        self.assertIn("30", command)
        self.assertIn("videos/test.mp4.mjpeg", command)
        self.assertIn("copy", command)

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
            audio_file="/run/medicam/videos-test.mp4.pcm",
            audio_lead_seconds=0.125,
        )

        self.assertEqual(command[command.index("-c:v") + 1], "copy")
        self.assertEqual(command[command.index("-c:a") + 1], "aac")
        self.assertEqual(command[command.index("-b:a") + 1], "128k")
        self.assertEqual(command[command.index("-ar") + 1], "48000")
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-ss") + 1], "0.125000")
        self.assertIn("s16le", command)
        self.assertIn("aresample=async=1:first_pts=0", command)
        self.assertIn("-shortest", command)
        self.assertNotIn("-an", command)
        self.assertNotIn("libx264", command)

    @patch("app.camera._remove_file")
    @patch("app.camera.open", new_callable=mock_open)
    @patch("app.camera.time.sleep")
    @patch("app.camera.time.monotonic", side_effect=[10.0, 11.0])
    @patch("app.camera.subprocess.Popen")
    def test_audio_capture_retries_a_transient_busy_device(
        self,
        popen_mock,
        _monotonic,
        sleep_mock,
        _open_mock,
        remove_file_mock,
    ):
        busy_process = Mock()
        busy_process.poll.return_value = 1
        active_process = Mock()
        active_process.poll.return_value = None
        popen_mock.side_effect = [busy_process, active_process]
        log = io.StringIO()

        process, started_at = camera._start_audio_capture(
            ["arecord", "test.wav"],
            log,
            "test.wav",
        )

        self.assertIs(process, active_process)
        self.assertEqual(started_at, 11.0)
        self.assertIn("retrying", log.getvalue())
        remove_file_mock.assert_called_once_with("test.wav")
        self.assertEqual(sleep_mock.call_count, 3)


class CameraLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_cwd = os.getcwd()
        self.old_state_file = camera.RECORDING_STATE_FILE
        os.chdir(self.tmp.name)
        os.makedirs("videos", exist_ok=True)
        camera.RECORDING_STATE_FILE = "videos/.recording-state.json"
        camera.capture_process = None
        camera.audio_process = None
        camera.ffmpeg_process = None
        camera.ffmpeg_log_file = None
        camera.recording_output_file = None
        camera.recording_raw_file = None
        camera.recording_audio_file = None
        camera.recording_audio_device = None
        camera.recording_audio_lead_seconds = 0.0
        camera.recording_remux_command = None
        camera.recording_phase = "idle"
        camera.recording_started_at_monotonic = None
        camera.recording_started_at_utc = None
        camera.recording_camera_device = None
        camera.recording_video_size = None
        camera.recording_fps = None
        camera.recording_capture_format = None
        camera.recording_generation = 0
        camera.last_recording_error = None
        camera.recovery_state_loaded = True

    def tearDown(self):
        camera.capture_process = None
        camera.audio_process = None
        camera.ffmpeg_process = None
        camera.ffmpeg_log_file = None
        camera.recording_output_file = None
        camera.recording_raw_file = None
        camera.recording_audio_file = None
        camera.recording_audio_device = None
        camera.recording_audio_lead_seconds = 0.0
        camera.recording_remux_command = None
        camera.recording_phase = "idle"
        camera.recording_started_at_monotonic = None
        camera.recording_started_at_utc = None
        camera.recording_camera_device = None
        camera.recording_video_size = None
        camera.recording_fps = None
        camera.recording_capture_format = None
        camera.last_recording_error = None
        camera.recovery_state_loaded = False
        camera.RECORDING_STATE_FILE = self.old_state_file
        os.chdir(self.old_cwd)
        self.tmp.cleanup()

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

    def test_fast_second_start_is_idempotent(self):
        process = Mock()
        process.poll.return_value = None
        camera.capture_process = process
        camera.recording_phase = "recording"
        camera.recording_output_file = "videos/active.mp4"

        response = camera.start_recording()

        self.assertEqual(response["status"], "already_recording")
        self.assertEqual(response["file"], "videos/active.mp4")

    def test_second_stop_during_finalization_is_idempotent(self):
        camera.recording_phase = "finalizing"
        camera.recording_output_file = "videos/active.mp4"

        response = camera.stop_recording()

        self.assertEqual(response["status"], "already_finalizing")
        self.assertEqual(response["file"], "videos/active.mp4")

    @patch("app.camera.platform.system", return_value="Linux")
    @patch("app.camera._find_linux_camera_device", return_value="/dev/video0")
    def test_health_status_reports_runtime_and_storage(
        self,
        _find_camera,
        _system,
    ):
        process = Mock()
        process.poll.return_value = None
        camera.capture_process = process
        camera.recording_phase = "recording"
        camera.recording_output_file = "videos/active.mp4"
        camera.recording_raw_file = "videos/active.mp4.mjpeg"
        camera.recording_started_at_monotonic = time.monotonic() - 12
        camera.recording_camera_device = "/dev/video0"
        camera.recording_video_size = "1920x1080"
        camera.recording_fps = "30"
        with open(camera.recording_raw_file, "wb") as raw:
            raw.write(b"frame-data")

        status = camera.get_recording_status()

        self.assertEqual(status["state"], "recording")
        self.assertTrue(status["recording"])
        self.assertTrue(status["capture_active"])
        self.assertGreaterEqual(status["duration_seconds"], 12)
        self.assertEqual(status["current_size_bytes"], 10)
        self.assertGreater(status["free_space_bytes"], 0)
        self.assertEqual(status["resolution"], "1920x1080")
        self.assertEqual(status["fps"], "30")
        self.assertTrue(status["camera"]["available"])

    @patch("app.camera.platform.system", return_value="Linux")
    @patch("app.camera._find_linux_camera_device", return_value="/dev/video0")
    def test_backend_restart_restores_raw_recording(
        self,
        _find_camera,
        _system,
    ):
        raw_file = "videos/interrupted.mp4.mjpeg"
        with open(raw_file, "wb") as raw:
            raw.write(b"recoverable frames")
        with open(camera.RECORDING_STATE_FILE, "w", encoding="utf-8") as state:
            json.dump(
                {
                    "phase": "recording",
                    "output_file": "videos/interrupted.mp4",
                    "raw_file": raw_file,
                    "started_at": "2026-01-01T00:00:00+00:00",
                    "video_size": "1920x1080",
                    "fps": "30",
                },
                state,
            )
        camera.recovery_state_loaded = False

        status = camera.get_recording_status()

        self.assertEqual(status["state"], "interrupted")
        self.assertTrue(status["recording"])
        self.assertTrue(status["recoverable"])
        self.assertEqual(status["last_error"]["code"], "backend_restarted")

    @patch("app.camera._probe_recording")
    @patch("app.camera.subprocess.run")
    def test_stop_finalizes_recovered_raw_video(self, run_mock, probe_mock):
        raw_file = "videos/interrupted.mp4.mjpeg"
        with open(raw_file, "wb") as raw:
            raw.write(b"recoverable frames")
        run_mock.return_value = Mock(returncode=0)
        probe_mock.return_value = {
            "valid": True,
            "healthy": True,
            "frame_count": 300,
            "expected_frames": 300,
            "avg_fps": 30.0,
        }
        camera.recording_phase = "interrupted"
        camera.recording_output_file = "videos/interrupted.mp4"
        camera.recording_raw_file = raw_file
        camera.recording_fps = "30"
        camera.recording_remux_command = ["ffmpeg", "recover"]

        response = camera.stop_recording()

        self.assertEqual(response["status"], "recording_stopped")
        self.assertEqual(response["returncode"], 0)
        self.assertTrue(response["recovered"])
        self.assertFalse(os.path.exists(raw_file))
        self.assertEqual(camera.recording_phase, "idle")

    @patch("app.camera.subprocess.run", return_value=Mock(returncode=1))
    def test_failed_recovery_preserves_raw_source(self, _run):
        raw_file = "videos/interrupted.mp4.mjpeg"
        with open(raw_file, "wb") as raw:
            raw.write(b"recoverable frames")
        camera.recording_phase = "interrupted"
        camera.recording_output_file = "videos/interrupted.mp4"
        camera.recording_raw_file = raw_file
        camera.recording_fps = "30"
        camera.recording_remux_command = ["ffmpeg", "recover"]

        response = camera.stop_recording()

        self.assertNotEqual(response["returncode"], 0)
        self.assertTrue(response["recoverable"])
        self.assertTrue(os.path.exists(raw_file))
        self.assertEqual(camera.recording_phase, "interrupted")

    @patch("app.camera._stop_capture_process")
    def test_video_disconnect_marks_interrupted_and_stops_audio(self, stop_mock):
        video = Mock()
        video.poll.return_value = 1
        microphone = Mock()
        microphone.poll.return_value = None
        camera.capture_process = video
        camera.audio_process = microphone
        camera.recording_phase = "recording"
        camera.recording_output_file = "videos/interrupted.mp4"
        camera.recording_raw_file = "videos/interrupted.mp4.mjpeg"

        camera._refresh_recording_state_locked()

        self.assertEqual(camera.recording_phase, "interrupted")
        self.assertEqual(camera.last_recording_error["code"], "video_capture_exited")
        stop_mock.assert_called_once_with(microphone)

    @patch("app.camera.shutil.disk_usage", return_value=Mock(free=0))
    @patch("app.camera._stop_capture_process")
    def test_storage_reserve_stops_capture_before_remux_space_is_lost(
        self,
        stop_mock,
        _disk_usage,
    ):
        video = Mock()
        video.poll.return_value = None
        camera.capture_process = video
        camera.recording_phase = "recording"
        camera.recording_output_file = "videos/storage.mp4"
        camera.recording_raw_file = "videos/storage.mp4.mjpeg"
        with open(camera.recording_raw_file, "wb") as raw:
            raw.write(b"frame")

        camera._refresh_recording_state_locked()

        self.assertEqual(camera.recording_phase, "interrupted")
        self.assertEqual(
            camera.last_recording_error["code"],
            "storage_reserve_reached",
        )
        stop_mock.assert_any_call(video)

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
