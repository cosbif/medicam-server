import io
import unittest
from unittest.mock import Mock, patch

from app import preview


def jpeg(payload: bytes) -> bytes:
    return b"\xff\xd8" + payload + b"\xff\xd9"


class PreviewFrameTests(unittest.TestCase):
    def test_split_jpegs_are_reassembled_without_decoding(self):
        buffer = bytearray(b"garbage" + jpeg(b"one") + b"\xff\xd8two")

        frames = list(preview._extract_mjpeg_frames(buffer))

        self.assertEqual(frames, [jpeg(b"one")])
        self.assertEqual(buffer, bytearray(b"\xff\xd8two"))
        buffer.extend(b"\xff\xd9")
        self.assertEqual(list(preview._extract_mjpeg_frames(buffer)), [jpeg(b"two")])

    def test_idle_preview_drops_one_of_every_five_camera_jpegs(self):
        stream = io.BytesIO(b"".join(jpeg(str(index).encode()) for index in range(10)))
        process = Mock()
        process.stdout = stream
        process.poll.return_value = 0
        manager = preview.PreviewManager(enabled=True)
        manager._camera_device = "/dev/video0"
        manager._producer_serial = 1

        with patch("app.preview.subprocess.Popen", return_value=process):
            manager._run_idle(1, preview.threading.Event())

        self.assertEqual(manager._frame_generation, 8)
        self.assertEqual(manager._latest_frame, jpeg(b"8"))

    def test_waiter_receives_only_the_latest_frame(self):
        manager = preview.PreviewManager(enabled=True)
        manager._producer_serial = 3
        manager._publish(3, jpeg(b"old"))
        previous_generation = manager._frame_generation
        manager._publish(3, jpeg(b"new"))

        generation, frame = manager.wait_for_frame(previous_generation, timeout=0.01)

        self.assertEqual(generation, previous_generation + 1)
        self.assertEqual(frame, jpeg(b"new"))


class PreviewCommandTests(unittest.TestCase):
    def test_product_default_keeps_preview_disconnected(self):
        self.assertFalse(preview.PREVIEW_ENABLED)

    def test_idle_command_stream_copies_native_sd_mjpeg(self):
        command = preview._idle_capture_command("/dev/video0")

        self.assertIn("640x360", command)
        self.assertIn("30", command)
        self.assertIn("copy", command)
        self.assertNotIn("scale=640:360:flags=fast_bilinear", command)
        self.assertEqual(command[:4], ["nice", "-n", "19", "ffmpeg"])

    def test_recording_preview_is_single_threaded_and_low_priority(self):
        command = preview._recording_transcode_command()

        self.assertEqual(command[:4], ["nice", "-n", "19", "ffmpeg"])
        self.assertIn("scale=640:360:flags=fast_bilinear", command)
        threads = command.index("-threads")
        self.assertEqual(command[threads + 1], "1")
        framerate = command.index("-framerate")
        self.assertEqual(command[framerate + 1], "24")

    def test_disabled_manager_never_starts_a_producer(self):
        manager = preview.PreviewManager(enabled=False)

        generation = manager.subscribe(camera_device="/dev/video0")

        self.assertEqual(generation, 0)
        self.assertFalse(manager.status()["active"])
        self.assertEqual(manager.status()["subscribers"], 1)
        manager.unsubscribe()


if __name__ == "__main__":
    unittest.main()
