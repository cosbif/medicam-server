"""ALSA microphone discovery, capture commands, and level measurement."""

from __future__ import annotations

import math
import re
import struct
import subprocess
import time


AUDIO_SAMPLE_RATE = 48_000
AUDIO_CHANNELS = 1
AUDIO_FORMAT = "S16_LE"
AUDIO_BITRATE = "128k"
LEVEL_SAMPLE_RATE = 16_000
MAX_LEVEL_TEST_SECONDS = 5
LEVEL_BUSY_ATTEMPTS = 4
LEVEL_BUSY_RETRY_DELAY = 0.25
AUDIO_BUFFER_TIME_US = 2_000_000
AUDIO_PERIOD_TIME_US = 250_000
SIGNAL_RMS_THRESHOLD_DBFS = -55.0
SIGNAL_PEAK_THRESHOLD_DBFS = -50.0

_ARECORD_DEVICE_RE = re.compile(
    r"^card\s+(?P<card_index>\d+):\s+"
    r"(?P<card_id>[^\s]+)\s+\[(?P<card_name>[^\]]+)\],\s+"
    r"device\s+(?P<device_index>\d+):\s+"
    r"(?P<device_id>.*?)\s+\[(?P<device_name>[^\]]+)\]\s*$"
)


class AudioError(RuntimeError):
    def __init__(self, code: str, details: str = ""):
        super().__init__(details or code)
        self.code = code
        self.details = details


def parse_arecord_devices(output: str) -> list[dict]:
    devices = []
    seen = set()
    for raw_line in (output or "").splitlines():
        match = _ARECORD_DEVICE_RE.match(raw_line.strip())
        if not match:
            continue

        values = match.groupdict()
        card_id = values["card_id"].strip()
        device_index = int(values["device_index"])
        alsa_device = f"plughw:CARD={card_id},DEV={device_index}"
        if alsa_device in seen:
            continue
        seen.add(alsa_device)

        card_name = values["card_name"].strip()
        device_name = values["device_name"].strip()
        label = card_name
        if device_name and device_name.lower() != card_name.lower():
            label = f"{card_name} — {device_name}"

        devices.append(
            {
                "id": alsa_device,
                "label": label,
                "card_id": card_id,
                "card_index": int(values["card_index"]),
                "device_index": device_index,
                "sample_rate": AUDIO_SAMPLE_RATE,
                "channels": AUDIO_CHANNELS,
            }
        )

    return devices


def list_capture_devices() -> list[dict]:
    try:
        result = subprocess.run(
            ["arecord", "-l"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_arecord_devices(result.stdout)


def resolve_capture_device(configured_device: str | None = "auto") -> dict | None:
    configured = (configured_device or "auto").strip()
    devices = list_capture_devices()
    if configured and configured != "auto":
        return next((item for item in devices if item["id"] == configured), None)
    return devices[0] if devices else None


def build_arecord_command(alsa_device: str) -> list[str]:
    return [
        "arecord",
        "-q",
        "-D", alsa_device,
        # Stream headerless PCM to stdout. The parent process owns the output
        # file, so a USB disconnect cannot make arecord unlink captured audio.
        "-t", "raw",
        "-f", AUDIO_FORMAT,
        "-r", str(AUDIO_SAMPLE_RATE),
        "-c", str(AUDIO_CHANNELS),
        f"--buffer-time={AUDIO_BUFFER_TIME_US}",
        f"--period-time={AUDIO_PERIOD_TIME_US}",
    ]


def measure_audio_level(
    configured_device: str | None = "auto",
    duration_seconds: int = 2,
) -> dict:
    duration = max(1, min(int(duration_seconds), MAX_LEVEL_TEST_SECONDS))
    device = resolve_capture_device(configured_device)
    if device is None:
        raise AudioError("audio_device_unavailable")

    command = [
        "arecord",
        "-q",
        "-D", device["id"],
        "-t", "raw",
        "-f", AUDIO_FORMAT,
        "-r", str(LEVEL_SAMPLE_RATE),
        "-c", str(AUDIO_CHANNELS),
        "-d", str(duration),
    ]
    for attempt in range(1, LEVEL_BUSY_ATTEMPTS + 1):
        try:
            result = subprocess.run(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=duration + 5,
                check=False,
            )
        except subprocess.TimeoutExpired as error:
            raise AudioError("audio_test_timeout") from error
        except OSError as error:
            raise AudioError("audio_capture_failed", str(error)) from error

        if result.returncode == 0:
            break

        details = result.stderr.decode("utf-8", errors="replace").strip()
        is_busy = "busy" in details.lower()
        if is_busy and attempt < LEVEL_BUSY_ATTEMPTS:
            time.sleep(LEVEL_BUSY_RETRY_DELAY)
            continue
        code = "audio_device_busy" if is_busy else "audio_capture_failed"
        raise AudioError(code, details)

    stats = calculate_pcm_s16le_stats(result.stdout)
    return {
        "status": "ok",
        "device": device,
        "duration_seconds": duration,
        **stats,
    }


def calculate_pcm_s16le_stats(data: bytes) -> dict:
    usable_length = len(data) - (len(data) % 2)
    if usable_length <= 0:
        raise AudioError("audio_no_samples")

    sample_count = usable_length // 2
    samples = (value[0] for value in struct.iter_unpack("<h", data[:usable_length]))
    square_sum = 0
    peak = 0
    clipped = 0
    for sample in samples:
        absolute = abs(sample)
        square_sum += sample * sample
        peak = max(peak, absolute)
        if absolute >= 32_760:
            clipped += 1

    rms = math.sqrt(square_sum / sample_count)
    rms_dbfs = _to_dbfs(rms)
    peak_dbfs = _to_dbfs(peak)
    return {
        "sample_count": sample_count,
        "rms_dbfs": round(rms_dbfs, 1),
        "peak_dbfs": round(peak_dbfs, 1),
        "clipped_percent": round(clipped * 100 / sample_count, 3),
        "signal_detected": (
            rms_dbfs >= SIGNAL_RMS_THRESHOLD_DBFS
            or peak_dbfs >= SIGNAL_PEAK_THRESHOLD_DBFS
        ),
    }


def _to_dbfs(value: float) -> float:
    if value <= 0:
        return -96.0
    return max(-96.0, 20.0 * math.log10(value / 32768.0))
