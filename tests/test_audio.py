import math
import struct
import unittest
from unittest.mock import Mock, patch

from app import audio


ARECORD_OUTPUT = """**** List of CAPTURE Hardware Devices ****
card 1: HD [Usb HD], device 0: USB Audio [USB Audio]
card 2: Lav [Wireless Microphone], device 0: USB Audio [USB Audio]
"""


class AudioDiscoveryTests(unittest.TestCase):
    def test_arecord_devices_use_stable_card_ids(self):
        devices = audio.parse_arecord_devices(ARECORD_OUTPUT)

        self.assertEqual(len(devices), 2)
        self.assertEqual(devices[0]["id"], "plughw:CARD=HD,DEV=0")
        self.assertEqual(devices[0]["label"], "Usb HD — USB Audio")
        self.assertEqual(devices[1]["id"], "plughw:CARD=Lav,DEV=0")
        self.assertEqual(devices[1]["sample_rate"], 48_000)
        self.assertEqual(devices[1]["channels"], 1)

    @patch("app.audio.subprocess.run")
    def test_list_capture_devices_handles_arecord_output(self, run_mock):
        run_mock.return_value = Mock(returncode=0, stdout=ARECORD_OUTPUT)

        devices = audio.list_capture_devices()

        self.assertEqual([item["card_id"] for item in devices], ["HD", "Lav"])

    def test_recording_command_captures_mono_48khz_wav(self):
        command = audio.build_arecord_command(
            "videos/test.mp4.wav",
            "plughw:CARD=HD,DEV=0",
        )

        self.assertEqual(command[0], "arecord")
        self.assertEqual(command[command.index("-D") + 1], "plughw:CARD=HD,DEV=0")
        self.assertEqual(command[command.index("-t") + 1], "wav")
        self.assertEqual(command[command.index("-f") + 1], "S16_LE")
        self.assertEqual(command[command.index("-r") + 1], "48000")
        self.assertEqual(command[command.index("-c") + 1], "1")
        self.assertIn("--buffer-time=2000000", command)
        self.assertIn("--period-time=250000", command)


class AudioLevelTests(unittest.TestCase):
    @staticmethod
    def _pcm(*samples):
        return b"".join(struct.pack("<h", sample) for sample in samples)

    def test_silence_is_reported_without_signal(self):
        stats = audio.calculate_pcm_s16le_stats(self._pcm(*([0] * 100)))

        self.assertEqual(stats["rms_dbfs"], -96.0)
        self.assertEqual(stats["peak_dbfs"], -96.0)
        self.assertFalse(stats["signal_detected"])
        self.assertEqual(stats["clipped_percent"], 0.0)

    def test_audible_signal_level_is_calculated(self):
        amplitude = 8_192
        samples = [amplitude, -amplitude] * 100
        stats = audio.calculate_pcm_s16le_stats(self._pcm(*samples))

        expected_dbfs = 20 * math.log10(amplitude / 32_768)
        self.assertAlmostEqual(stats["rms_dbfs"], expected_dbfs, places=1)
        self.assertAlmostEqual(stats["peak_dbfs"], expected_dbfs, places=1)
        self.assertTrue(stats["signal_detected"])

    def test_clipping_percentage_is_reported(self):
        stats = audio.calculate_pcm_s16le_stats(
            self._pcm(32_767, -32_768, 1_000, -1_000)
        )

        self.assertEqual(stats["clipped_percent"], 50.0)

    def test_empty_audio_is_rejected(self):
        with self.assertRaises(audio.AudioError) as context:
            audio.calculate_pcm_s16le_stats(b"")

        self.assertEqual(context.exception.code, "audio_no_samples")

    @patch("app.audio.time.sleep")
    @patch("app.audio.subprocess.run")
    @patch("app.audio.resolve_capture_device")
    def test_level_measurement_retries_a_transient_busy_device(
        self,
        resolve_mock,
        run_mock,
        sleep_mock,
    ):
        resolve_mock.return_value = {
            "id": "plughw:CARD=HD,DEV=0",
            "label": "USB microphone",
        }
        run_mock.side_effect = [
            Mock(returncode=1, stdout=b"", stderr=b"Device or resource busy"),
            Mock(returncode=0, stdout=self._pcm(4_096, -4_096), stderr=b""),
        ]

        result = audio.measure_audio_level(duration_seconds=1)

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["signal_detected"])
        self.assertEqual(run_mock.call_count, 2)
        sleep_mock.assert_called_once_with(audio.LEVEL_BUSY_RETRY_DELAY)


if __name__ == "__main__":
    unittest.main()
