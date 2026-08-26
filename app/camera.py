'''Camera settings and video recording lifecycle.'''

from fastapi import HTTPException
from datetime import datetime, timezone
import glob
import json
import os
import platform
import shlex
import signal
import shutil
import stat
import subprocess
import threading
import time

from app import audio, preview, storage_manager, utils


SETTINGS_FILE = "camera_settings.json"
FFMPEG_LOG_FILE = "ffmpeg.log"
CAMERA_DISCOVERY_TIMEOUT = 3.0
FFMPEG_STARTUP_DELAY = 1.0
CAPTURE_START_OBSERVATION_TIMEOUT = 3.0
FFMPEG_STOP_TIMEOUT = 10.0
FFMPEG_REMUX_TIMEOUT = 180.0
REMUX_MIN_THROUGHPUT_BYTES_PER_SECOND = 4 * 1024 * 1024
PROBE_MIN_THROUGHPUT_BYTES_PER_SECOND = 2 * 1024 * 1024
FILE_PROCESSING_TIMEOUT_MARGIN = 120.0
AUDIO_OPEN_ATTEMPTS = 8
AUDIO_DATA_START_TIMEOUT = 3.0
AUDIO_DATA_POLL_INTERVAL = 0.02
AUDIO_OPEN_RETRY_DELAY = 0.35
AUDIO_BYTES_PER_SAMPLE = 2
AUDIO_TEMP_DIR = os.environ.get("MEDICAM_AUDIO_TEMP_DIR", "/run/medicam")
RECORDING_STATE_FILE = os.environ.get(
    "MEDICAM_RECORDING_STATE_FILE",
    os.path.join(utils.VIDEOS_DIR, ".recording-state.json"),
)
MIN_RECORDING_FREE_BYTES = int(
    os.environ.get("MEDICAM_MIN_RECORDING_FREE_BYTES", 1024 * 1024 * 1024)
)
WATCHDOG_INTERVAL_SECONDS = 0.5
HEALTHY_FRAME_DELIVERY_RATIO = 0.995
HEALTHY_AVG_FPS = 29.5
V4L2_CONTROL_TIMEOUT = 5.0
FULLHD30_EXPOSURE_LOCK_CAMERAS = {("32e4", "0415")}

camera_settings = {
    "resolution": "FHD",
    "fps": "30",
    "audio_enabled": True,
    "audio_device": "auto",
}

# FullHD is intentionally the maximum supported resolution. The Linux recorder
# keeps CPU usage low by stream copying the camera's FullHD MJPEG stream and
# writing a normalized 30 fps output file.
SUPPORTED_RESOLUTIONS = {
    "SD": "640x360",
    "HD": "1280x720",
    "FHD": "1920x1080",
}

LEGACY_RESOLUTION_MAP = {
    value: key for key, value in SUPPORTED_RESOLUTIONS.items()
}

SUPPORTED_FPS = {"30"}
LEGACY_FPS_MAP = {"15": "30", "60": "30"}
LINUX_INPUT_QUEUE_SIZE = "1024"

capture_process = None
audio_process = None
ffmpeg_process = None
ffmpeg_log_file = None
recording_output_file = None
recording_raw_file = None
recording_audio_file = None
recording_audio_device = None
recording_audio_lead_seconds = 0.0
recording_remux_command = None
recording_phase = "idle"
recording_started_at_monotonic = None
recording_started_at_utc = None
recording_camera_device = None
recording_camera_control_state = None
recording_video_size = None
recording_fps = None
recording_capture_format = None
recording_generation = 0
last_recording_error = None
recovery_state_loaded = False
recording_lock = threading.RLock()


def _preview_call(method: str, *args) -> None:
    """Keep every preview failure outside the recording control path."""
    try:
        getattr(preview, method)(*args)
    except Exception:
        # Preview is optional by design. A malformed frame, failed helper
        # process, or shutdown race must never prevent FullHD capture.
        pass


def _normalize_settings(settings: dict | None):
    settings = settings or {}

    resolution = str(settings.get("resolution", camera_settings["resolution"]))
    resolution = LEGACY_RESOLUTION_MAP.get(resolution, resolution)
    if resolution not in SUPPORTED_RESOLUTIONS:
        resolution = "FHD"

    fps = str(settings.get("fps", camera_settings["fps"]))
    fps = LEGACY_FPS_MAP.get(fps, fps)
    if fps not in SUPPORTED_FPS:
        fps = "30"

    raw_audio_enabled = settings.get(
        "audio_enabled",
        camera_settings["audio_enabled"],
    )
    if isinstance(raw_audio_enabled, str):
        audio_enabled = raw_audio_enabled.strip().lower() in {
            "1",
            "true",
            "yes",
            "on",
        }
    else:
        audio_enabled = bool(raw_audio_enabled)

    audio_device = str(
        settings.get("audio_device", camera_settings["audio_device"])
        or "auto"
    ).strip()

    return {
        "resolution": resolution,
        "fps": fps,
        "audio_enabled": audio_enabled,
        "audio_device": audio_device or "auto",
    }


def _is_character_device(path: str) -> bool:
    try:
        return stat.S_ISCHR(os.stat(path).st_mode)
    except OSError:
        return False


def _camera_candidates():
    configured_device = os.environ.get("MEDICAM_CAMERA_DEVICE")
    if configured_device:
        return [configured_device]

    # The by-id link follows the UVC capture node even when /dev/videoN changes
    # after a USB reconnect. video-index0 is the image stream; index1 is metadata.
    candidates = sorted(glob.glob("/dev/v4l/by-id/*-video-index0"))
    candidates.extend(sorted(glob.glob("/dev/video[0-9]*")))

    deduplicated = []
    seen_targets = set()
    for path in candidates:
        real_path = os.path.realpath(path)
        if real_path not in seen_targets:
            seen_targets.add(real_path)
            deduplicated.append(path)
    return deduplicated


def _find_linux_camera_device(timeout: float = CAMERA_DISCOVERY_TIMEOUT):
    deadline = time.monotonic() + timeout
    while True:
        for path in _camera_candidates():
            if _is_character_device(path):
                return path

        if time.monotonic() >= deadline:
            return None
        time.sleep(0.25)


def find_camera_device(timeout: float = CAMERA_DISCOVERY_TIMEOUT):
    """Return the stable Linux camera path for diagnostics and recording."""
    if platform.system() == "Linux":
        return _find_linux_camera_device(timeout=timeout)
    if platform.system() == "Windows":
        return "video=AT025"
    return None


def _camera_usb_identity(camera_device: str) -> tuple[str, str] | None:
    try:
        result = subprocess.run(
            [
                "udevadm",
                "info",
                "--query=property",
                f"--name={os.path.realpath(camera_device)}",
            ],
            capture_output=True,
            text=True,
            timeout=V4L2_CONTROL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    properties = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            properties[key] = value.strip().lower()
    vendor_id = properties.get("ID_VENDOR_ID")
    model_id = properties.get("ID_MODEL_ID")
    return (vendor_id, model_id) if vendor_id and model_id else None


def _read_v4l2_controls(camera_device: str) -> dict[str, int] | None:
    try:
        result = subprocess.run(
            [
                "v4l2-ctl",
                "-d",
                camera_device,
                "--get-ctrl=auto_exposure,exposure_time_absolute",
            ],
            capture_output=True,
            text=True,
            timeout=V4L2_CONTROL_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    controls = {}
    for line in result.stdout.splitlines():
        name, separator, raw_value = line.partition(":")
        value = raw_value.strip().split(maxsplit=1)[0] if separator else ""
        try:
            controls[name.strip()] = int(value)
        except ValueError:
            continue
    required = {"auto_exposure", "exposure_time_absolute"}
    return controls if required.issubset(controls) else None


def _set_v4l2_control(camera_device: str, name: str, value: int) -> None:
    result = subprocess.run(
        [
            "v4l2-ctl",
            "-d",
            camera_device,
            f"--set-ctrl={name}={int(value)}",
        ],
        capture_output=True,
        text=True,
        timeout=V4L2_CONTROL_TIMEOUT,
        check=False,
    )
    if result.returncode != 0:
        raise OSError(result.stderr.strip() or f"failed to set {name}")


def _lock_recording_exposure(
    camera_device: str,
    video_size: str,
    fps: str,
) -> dict | None:
    identity = _camera_usb_identity(camera_device)
    if (
        identity not in FULLHD30_EXPOSURE_LOCK_CAMERAS
        or video_size != "1920x1080"
        or str(fps) != "30"
    ):
        return None
    state = {
        "required": True,
        "applied": False,
        "device": camera_device,
        "vendor_id": identity[0],
        "model_id": identity[1],
        "mode": "locked_current_exposure",
    }
    controls = _read_v4l2_controls(camera_device)
    if controls is None:
        state["error"] = "camera_exposure_controls_unavailable"
        return state
    state.update(
        {
            "original_auto_exposure": controls["auto_exposure"],
            "original_exposure_time_absolute": controls[
                "exposure_time_absolute"
            ],
        }
    )
    try:
        _set_v4l2_control(camera_device, "auto_exposure", 1)
        state["applied"] = True
        _set_v4l2_control(
            camera_device,
            "exposure_time_absolute",
            controls["exposure_time_absolute"],
        )
    except (OSError, subprocess.SubprocessError) as error:
        state["error"] = f"camera_exposure_lock_failed: {error}"
        _restore_recording_exposure(state)
        state["applied"] = False
        return state
    return state


def _restore_recording_exposure(state: dict | None) -> bool:
    if not isinstance(state, dict) or not state.get("applied"):
        return False
    camera_device = str(state.get("device") or "")
    real_device = os.path.realpath(camera_device)
    original_auto = state.get("original_auto_exposure")
    original_exposure = state.get("original_exposure_time_absolute")
    if (
        not real_device.startswith("/dev/video")
        or not _is_character_device(camera_device)
        or original_auto not in {1, 3}
        or not isinstance(original_exposure, int)
        or not 1 <= original_exposure <= 8188
    ):
        return False
    try:
        _set_v4l2_control(camera_device, "auto_exposure", 1)
        _set_v4l2_control(
            camera_device,
            "exposure_time_absolute",
            original_exposure,
        )
        _set_v4l2_control(camera_device, "auto_exposure", original_auto)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def _public_camera_control_state(state: dict | None) -> dict | None:
    if not isinstance(state, dict):
        return None
    return {
        key: state[key]
        for key in ("required", "applied", "mode", "vendor_id", "model_id", "error")
        if key in state
    }


def _release_recording_camera_controls() -> None:
    global recording_camera_control_state

    _restore_recording_exposure(recording_camera_control_state)
    recording_camera_control_state = None


def count_mjpeg_frames(path: str) -> int:
    """Count complete JPEG frames in a raw MJPEG file without decoding them."""
    return _count_mjpeg_frames(path)


def _build_audio_temp_file(output_file: str):
    # PCM is tiny compared with FullHD MJPEG but frequent synchronous writes to
    # the same microSD can starve ALSA for several seconds. systemd creates the
    # /run/medicam tmpfs directory, which is not subject to logind RemoveIPC.
    # Fall back to the video folder if the runtime directory is unavailable.
    temp_dir = AUDIO_TEMP_DIR
    if not os.path.isdir(temp_dir) or not os.access(temp_dir, os.W_OK):
        temp_dir = os.path.dirname(output_file) or "."
    return os.path.join(temp_dir, f"medicam-{os.path.basename(output_file)}.pcm")


def _build_linux_capture_command(
    video_size: str,
    fps: str,
    raw_file: str,
    camera_device: str,
):
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostats",
        "-y",
        # v4l2-ctl loses large groups of buffers with this UVC device behind
        # the Type-C hub. FFmpeg's dedicated v4l2 reader sustained exactly
        # 900/900 frames in the same 30-second FullHD microSD test.
        "-thread_queue_size", LINUX_INPUT_QUEUE_SIZE,
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-framerate", fps,
        "-video_size", video_size,
        "-i", camera_device,
        "-map", "0:v:0",
        "-c:v", "copy",
        "-an",
        "-f", "mjpeg",
        "-flush_packets", "1",
        raw_file,
    ]


def _build_linux_command(
    raw_file: str,
    fps: str,
    output_file: str,
    audio_file: str | None = None,
    audio_lead_seconds: float = 0.0,
):
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostats",
        "-y",
        "-f", "mjpeg",
        "-framerate", fps,
        "-i", raw_file,
    ]
    if audio_file:
        command.extend([
            "-f", "s16le",
            "-ar", str(audio.AUDIO_SAMPLE_RATE),
            "-ac", str(audio.AUDIO_CHANNELS),
        ])
        # This USB camera only allows simultaneous UVC and ALSA capture when
        # the audio interface is opened first. Drop that short audio lead so
        # both tracks begin at the same wall-clock moment in the MP4 file.
        if audio_lead_seconds > 0:
            command.extend(["-ss", f"{audio_lead_seconds:.6f}"])
        command.extend(["-i", audio_file])

    command.extend([
        "-map", "0:v:0",
        # The UVC camera already produces compressed MJPEG. FFmpeg only remuxes
        # the raw V4L2 MJPEG capture into MP4; it does not decode or re-encode.
        "-c:v", "copy",
    ])
    if audio_file:
        command.extend([
            "-map", "1:a:0",
            "-c:a", "aac",
            "-b:a", audio.AUDIO_BITRATE,
            "-ar", str(audio.AUDIO_SAMPLE_RATE),
            "-ac", str(audio.AUDIO_CHANNELS),
            # Audio is not allowed to shorten an otherwise intact video. USB
            # capture can occasionally miss samples while UVC initializes;
            # aresample repairs timestamp drift and apad fills only the
            # missing tail with silence until the video stream ends.
            "-af", "aresample=async=1:first_pts=0,apad",
            "-shortest",
        ])
    else:
        command.append("-an")
    command.append(output_file)
    return command


def _build_windows_command(video_size: str, fps: str, output_file: str):
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-f", "dshow",
        "-framerate", fps,
        "-video_size", video_size,
        "-vcodec", "mjpeg",
        "-i", "video=AT025",
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        output_file,
    ]


def _start_audio_capture(command, log_file, audio_file):
    """Open ALSA before UVC and timestamp the first real PCM samples."""
    last_return_code = None
    for attempt in range(1, AUDIO_OPEN_ATTEMPTS + 1):
        with open(audio_file, "wb") as audio_output:
            process = subprocess.Popen(
                command,
                stdout=audio_output,
                stderr=log_file,
            )
        launched_at = time.monotonic()
        deadline = launched_at + AUDIO_DATA_START_TIMEOUT
        while True:
            last_return_code = process.poll()
            if last_return_code is not None:
                break

            captured_bytes = _safe_file_size(audio_file)
            if captured_bytes > 0:
                observed_at = time.monotonic()
                captured_seconds = captured_bytes / (
                    audio.AUDIO_SAMPLE_RATE
                    * audio.AUDIO_CHANNELS
                    * AUDIO_BYTES_PER_SAMPLE
                )
                # arecord writes complete ALSA periods. Subtracting the data
                # already present estimates when capture actually began and
                # avoids treating device-open latency as recorded audio.
                started_at = max(launched_at, observed_at - captured_seconds)
                return process, started_at

            if time.monotonic() >= deadline:
                last_return_code = _stop_capture_process(process)
                log_file.write(
                    f"[WARN] Audio open attempt {attempt} produced no PCM "
                    f"within {AUDIO_DATA_START_TIMEOUT:.1f}s\n"
                )
                log_file.flush()
                break
            time.sleep(AUDIO_DATA_POLL_INTERVAL)

        _remove_file(audio_file)
        if attempt < AUDIO_OPEN_ATTEMPTS:
            log_file.write(
                f"[WARN] Audio open attempt {attempt} exited with "
                f"code {last_return_code}; retrying\n"
            )
            log_file.flush()
            time.sleep(AUDIO_OPEN_RETRY_DELAY)

    raise audio.AudioError(
        "audio_capture_failed",
        f"Audio input did not open after {AUDIO_OPEN_ATTEMPTS} attempts "
        f"(last exit code {last_return_code})",
    )


def _close_process_resources(process):
    global ffmpeg_log_file

    if process is not None and process.stdin is not None:
        try:
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    if ffmpeg_log_file is not None:
        try:
            ffmpeg_log_file.close()
        finally:
            ffmpeg_log_file = None


def _clear_recording_state():
    global capture_process, audio_process, ffmpeg_process
    global recording_output_file, recording_raw_file, recording_audio_file
    global recording_audio_device, recording_audio_lead_seconds
    global recording_remux_command, recording_phase
    global recording_started_at_monotonic, recording_started_at_utc
    global recording_camera_device, recording_video_size, recording_fps
    global recording_camera_control_state
    global recording_capture_format, recording_generation

    _release_recording_camera_controls()
    capture_process = None
    audio_process = None
    ffmpeg_process = None
    recording_output_file = None
    recording_raw_file = None
    recording_audio_file = None
    recording_audio_device = None
    recording_audio_lead_seconds = 0.0
    recording_remux_command = None
    recording_phase = "idle"
    recording_started_at_monotonic = None
    recording_started_at_utc = None
    recording_camera_device = None
    recording_camera_control_state = None
    recording_video_size = None
    recording_fps = None
    recording_capture_format = None
    recording_generation += 1


def _stop_capture_process(process, timeout: float = 3.0):
    if process is None:
        return None

    if process.stdout is not None and not process.stdout.closed:
        try:
            process.stdout.close()
        except OSError:
            pass

    return_code = process.poll()
    if return_code is not None:
        return return_code

    try:
        process.send_signal(signal.SIGINT)
        return process.wait(timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.terminate()
        except ProcessLookupError:
            return process.poll()

    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        return process.wait(timeout=timeout)


def _remove_file(path: str | None):
    if not path:
        return
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _log_tail(max_lines: int = 20):
    try:
        with open(FFMPEG_LOG_FILE, "r", encoding="utf-8", errors="replace") as log:
            return "".join(log.readlines()[-max_lines:]).strip()
    except OSError:
        return ""


def _utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def _path_is_within(path: str | None, directory: str):
    if not path:
        return False
    try:
        return os.path.commonpath(
            [os.path.realpath(path), os.path.realpath(directory)]
        ) == os.path.realpath(directory)
    except (OSError, ValueError):
        return False


def _safe_file_size(path: str | None):
    if not path:
        return 0
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


def _file_processing_timeout(
    path: str | None,
    minimum_seconds: float,
    minimum_throughput: float,
):
    estimated = _safe_file_size(path) / max(1.0, minimum_throughput)
    return max(minimum_seconds, estimated + FILE_PROCESSING_TIMEOUT_MARGIN)


def _count_mjpeg_frames(path: str):
    count = 0
    previous = b""
    try:
        with open(path, "rb") as raw_file:
            while chunk := raw_file.read(1024 * 1024):
                data = previous + chunk
                count += data.count(b"\xff\xd8")
                previous = data[-1:]
    except OSError:
        return 0
    return count


def _wait_for_first_capture_byte(
    path: str,
    process,
    launched_at: float,
    fps: float,
):
    """Return the closest observable timestamp to the first captured frame."""
    deadline = launched_at + CAPTURE_START_OBSERVATION_TIMEOUT
    while time.monotonic() < deadline:
        if _safe_file_size(path) > 0:
            frames = _count_mjpeg_frames(path)
            observed_at = time.monotonic()
            if frames > 0 and fps > 0:
                return max(launched_at, observed_at - (frames / fps))
            return observed_at
        if process.poll() is not None:
            break
        time.sleep(0.01)
    return launched_at


def _recording_duration_seconds():
    if recording_started_at_monotonic is not None:
        return max(0.0, time.monotonic() - recording_started_at_monotonic)
    if recording_started_at_utc:
        try:
            started = datetime.fromisoformat(recording_started_at_utc)
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            return max(
                0.0,
                (datetime.now(timezone.utc) - started).total_seconds(),
            )
        except (TypeError, ValueError):
            pass
    return 0.0


def _set_last_error_locked(code: str, message: str, recoverable: bool = False):
    global last_recording_error
    last_recording_error = {
        "code": code,
        "message": str(message),
        "at": _utc_now_iso(),
        "recoverable": bool(recoverable),
    }


def _persist_recording_state_locked():
    os.makedirs(os.path.dirname(RECORDING_STATE_FILE) or ".", exist_ok=True)
    if recording_phase == "idle" and last_recording_error is None:
        _remove_file(RECORDING_STATE_FILE)
        _remove_file(f"{RECORDING_STATE_FILE}.tmp")
        return

    payload = {
        "version": 1,
        "phase": recording_phase,
        "output_file": recording_output_file,
        "raw_file": recording_raw_file,
        "audio_file": recording_audio_file,
        "audio_device": recording_audio_device,
        "audio_lead_seconds": recording_audio_lead_seconds,
        "started_at": recording_started_at_utc,
        "camera_device": recording_camera_device,
        "camera_control_state": recording_camera_control_state,
        "video_size": recording_video_size,
        "fps": recording_fps,
        "capture_format": recording_capture_format,
        "last_error": last_recording_error,
        "updated_at": _utc_now_iso(),
    }
    temporary = f"{RECORDING_STATE_FILE}.tmp"
    with open(temporary, "w", encoding="utf-8") as state_file:
        json.dump(payload, state_file, ensure_ascii=False)
        state_file.flush()
        os.fsync(state_file.fileno())
    os.replace(temporary, RECORDING_STATE_FILE)


def _restore_recording_state_locked():
    global recovery_state_loaded, recording_phase
    global recording_output_file, recording_raw_file, recording_audio_file
    global recording_audio_device, recording_audio_lead_seconds
    global recording_remux_command, recording_started_at_utc
    global recording_camera_device, recording_video_size, recording_fps
    global recording_camera_control_state
    global recording_capture_format, last_recording_error

    if recovery_state_loaded:
        return
    recovery_state_loaded = True

    saved = {}
    try:
        with open(RECORDING_STATE_FILE, "r", encoding="utf-8") as state_file:
            loaded = json.load(state_file)
            if isinstance(loaded, dict):
                saved = loaded
    except (OSError, json.JSONDecodeError, TypeError):
        saved = {}

    saved_error = saved.get("last_error")
    if isinstance(saved_error, dict):
        last_recording_error = dict(saved_error)

    saved_camera_control_state = saved.get("camera_control_state")
    if isinstance(saved_camera_control_state, dict):
        recording_camera_control_state = dict(saved_camera_control_state)
        _release_recording_camera_controls()

    output_file = saved.get("output_file")
    if not _path_is_within(output_file, utils.VIDEOS_DIR):
        output_file = None
    if output_file and not output_file.lower().endswith(".mp4"):
        output_file = None

    raw_file = f"{output_file}.mjpeg" if output_file else None
    if not raw_file or not os.path.isfile(raw_file):
        orphaned = sorted(
            glob.glob(os.path.join(utils.VIDEOS_DIR, "*.mp4.mjpeg")),
            key=lambda path: os.path.getmtime(path),
        )
        raw_file = orphaned[-1] if orphaned else None
        output_file = raw_file[:-len(".mjpeg")] if raw_file else None

    if raw_file and output_file:
        audio_file = saved.get("audio_file")
        valid_audio_location = (
            _path_is_within(audio_file, AUDIO_TEMP_DIR)
            or _path_is_within(audio_file, utils.VIDEOS_DIR)
        )
        if not valid_audio_location or not os.path.isfile(audio_file):
            audio_file = None

        recording_output_file = output_file
        recording_raw_file = raw_file
        recording_audio_file = audio_file
        recording_audio_device = saved.get("audio_device")
        recording_audio_lead_seconds = float(
            saved.get("audio_lead_seconds") or 0.0
        )
        recording_started_at_utc = saved.get("started_at")
        if not recording_started_at_utc:
            recording_started_at_utc = datetime.fromtimestamp(
                os.path.getmtime(raw_file), timezone.utc
            ).isoformat()
        recording_camera_device = saved.get("camera_device")
        recording_video_size = saved.get("video_size") or "1920x1080"
        recording_fps = str(saved.get("fps") or "30")
        recording_capture_format = (
            saved.get("capture_format") or "ffmpeg_v4l2_mjpeg_raw"
        )
        recording_remux_command = _build_linux_command(
            raw_file,
            recording_fps,
            output_file,
            audio_file=audio_file,
            audio_lead_seconds=recording_audio_lead_seconds,
        )
        recording_phase = "interrupted"
        if last_recording_error is None:
            _set_last_error_locked(
                "backend_restarted",
                "Backend restarted before the recording was finalized",
                recoverable=True,
            )
        _persist_recording_state_locked()
        return

    if saved.get("phase") in {"starting", "recording", "finalizing", "interrupted"}:
        recording_phase = "idle"
        _set_last_error_locked(
            "recovery_source_missing",
            "Interrupted recording source file is no longer available",
            recoverable=False,
        )
        _persist_recording_state_locked()


def _parse_rate(value):
    try:
        numerator, denominator = str(value).split("/", maxsplit=1)
        denominator_value = float(denominator)
        return float(numerator) / denominator_value if denominator_value else 0.0
    except (TypeError, ValueError):
        return 0.0


def _run_ffprobe(path: str, count_frames: bool = False):
    frame_field = "nb_read_frames" if count_frames else "nb_frames"
    command = [
        "ffprobe",
        "-v", "error",
    ]
    if count_frames:
        command.append("-count_frames")
    command.extend([
        "-select_streams", "v:0",
        "-show_entries",
        f"stream={frame_field},avg_frame_rate,width,height:format=duration",
        "-of", "json",
        path,
    ])
    result = subprocess.run(
        command,
        text=True,
        capture_output=True,
        timeout=(
            _file_processing_timeout(
                path,
                60.0,
                PROBE_MIN_THROUGHPUT_BYTES_PER_SECOND,
            )
            if count_frames
            else 60.0
        ),
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout)


def _probe_recording(path: str, elapsed_seconds: float, expected_fps: float):
    try:
        payload = _run_ffprobe(path)
        streams = payload.get("streams") or []
        stream = streams[0] if streams else {}
        frame_value = stream.get("nb_frames")
        if not str(frame_value or "").isdigit():
            payload = _run_ffprobe(path, count_frames=True)
            streams = payload.get("streams") or []
            stream = streams[0] if streams else {}
            frame_value = stream.get("nb_read_frames")
        frame_count = int(frame_value or 0)
        duration = float((payload.get("format") or {}).get("duration") or 0.0)
        avg_fps = _parse_rate(stream.get("avg_frame_rate", "0/1"))
        expected_frames = max(0, round(elapsed_seconds * expected_fps))
        if expected_frames == 0 and duration > 0:
            # After a backend restart monotonic time is unavailable. The file
            # duration still lets us validate that the recovered stream is
            # decodable and maintains the configured frame rate.
            expected_frames = max(1, round(duration * expected_fps))
        missing_frames = max(0, expected_frames - frame_count)
        delivery_ratio = (
            min(1.0, frame_count / expected_frames) if expected_frames else 0.0
        )
        return {
            "valid": frame_count > 0 and duration > 0,
            "duration_seconds": round(duration, 3),
            "wall_duration_seconds": round(elapsed_seconds, 3),
            "frame_count": frame_count,
            "expected_frames": expected_frames,
            "missing_frames": missing_frames,
            "frame_delivery_ratio": round(delivery_ratio, 6),
            "avg_fps": round(avg_fps, 3),
            "resolution": (
                f"{stream.get('width')}x{stream.get('height')}"
                if stream.get("width") and stream.get("height")
                else ""
            ),
            "healthy": (
                frame_count > 0
                and duration > 0
                and delivery_ratio >= HEALTHY_FRAME_DELIVERY_RATIO
                and avg_fps >= min(expected_fps - 0.5, HEALTHY_AVG_FPS)
            ),
        }
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        RuntimeError,
        subprocess.SubprocessError,
    ) as error:
        return {
            "valid": False,
            "healthy": False,
            "error": str(error),
        }


def _mark_interrupted_locked(code: str, message: str):
    global recording_phase
    if recording_phase != "recording":
        return
    recording_phase = "interrupted"
    _set_last_error_locked(code, message, recoverable=True)
    _persist_recording_state_locked()


def _refresh_recording_state_locked():
    if recording_phase != "recording":
        return
    video_process = ffmpeg_process or capture_process
    if video_process is None:
        _mark_interrupted_locked(
            "video_capture_missing",
            "Video capture process disappeared unexpectedly",
        )
        _stop_capture_process(audio_process)
        _release_recording_camera_controls()
        return
    video_return_code = video_process.poll()
    if video_return_code is not None:
        _mark_interrupted_locked(
            "video_capture_exited",
            f"Video capture exited unexpectedly with code {video_return_code}",
        )
        _stop_capture_process(audio_process)
        _release_recording_camera_controls()
        return
    try:
        disk_free = shutil.disk_usage(utils.VIDEOS_DIR).free
    except OSError:
        disk_free = MIN_RECORDING_FREE_BYTES
    finalization_reserve = (
        _safe_file_size(recording_raw_file) + MIN_RECORDING_FREE_BYTES
    )
    if disk_free < finalization_reserve:
        _mark_interrupted_locked(
            "storage_reserve_reached",
            "Recording stopped before the disk space required for MP4 finalization was exhausted",
        )
        _stop_capture_process(video_process)
        _stop_capture_process(audio_process)
        _release_recording_camera_controls()
        return
    if recording_audio_file is not None:
        if audio_process is None:
            _mark_interrupted_locked(
                "audio_capture_missing",
                "Audio capture process disappeared unexpectedly",
            )
            _stop_capture_process(video_process)
            _release_recording_camera_controls()
            return
        audio_return_code = audio_process.poll()
        if audio_return_code is not None:
            _mark_interrupted_locked(
                "audio_capture_exited",
                f"Audio capture exited unexpectedly with code {audio_return_code}",
            )
            _stop_capture_process(video_process)
            _release_recording_camera_controls()


def _watch_recording(generation: int):
    while True:
        time.sleep(WATCHDOG_INTERVAL_SECONDS)
        processes_to_stop = []
        with recording_lock:
            if generation != recording_generation or recording_phase != "recording":
                return
            previous_phase = recording_phase
            _refresh_recording_state_locked()
            if previous_phase == "recording" and recording_phase == "interrupted":
                processes_to_stop = [capture_process, audio_process, ffmpeg_process]
        if processes_to_stop:
            for process in processes_to_stop:
                _stop_capture_process(process)
            with recording_lock:
                _release_recording_camera_controls()
            _preview_call("recording_stopped")
            _preview_call("recording_finished")
            return


def _start_watchdog_locked():
    thread = threading.Thread(
        target=_watch_recording,
        args=(recording_generation,),
        name=f"medicam-recording-watchdog-{recording_generation}",
        daemon=True,
    )
    thread.start()


if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            camera_settings.update(_normalize_settings(json.load(settings_file)))
    except (OSError, json.JSONDecodeError, TypeError):
        pass


def start_recording():
    global capture_process, audio_process, ffmpeg_process, ffmpeg_log_file
    global recording_output_file, recording_raw_file, recording_audio_file
    global recording_audio_device, recording_audio_lead_seconds
    global recording_remux_command, recording_phase
    global recording_started_at_monotonic, recording_started_at_utc
    global recording_camera_device, recording_video_size, recording_fps
    global recording_camera_control_state
    global recording_capture_format, recording_generation
    global last_recording_error

    with recording_lock:
        _restore_recording_state_locked()
        _refresh_recording_state_locked()
        processes = [
            process
            for process in (ffmpeg_process, capture_process, audio_process)
            if process is not None
        ]
        if recording_phase == "finalizing":
            return {
                "status": "already_finalizing",
                "file": recording_output_file,
            }
        if recording_phase == "interrupted" or (
            recording_raw_file and os.path.isfile(recording_raw_file)
        ):
            return {
                "status": "recovery_required",
                "file": recording_output_file,
                "details": "Finalize the interrupted recording with /stop before starting a new one",
            }
        if processes and any(process.poll() is None for process in processes):
            return {
                "status": "already_recording",
                "file": recording_output_file,
            }
        if processes:
            _clear_recording_state()

        resolution_key = camera_settings.get("resolution", "FHD")
        video_size = SUPPORTED_RESOLUTIONS.get(resolution_key)
        fps = str(camera_settings.get("fps", "30"))
        audio_enabled = bool(camera_settings.get("audio_enabled", True))
        if not video_size or fps not in SUPPORTED_FPS:
            normalized = _normalize_settings(camera_settings)
            camera_settings.update(normalized)
            resolution_key = normalized["resolution"]
            video_size = SUPPORTED_RESOLUTIONS[resolution_key]
            fps = normalized["fps"]

        storage_cleanup = storage_manager.apply_policy(trigger="recording_start")
        system = platform.system()
        output_file = utils.get_output_filename()
        free_bytes = shutil.disk_usage(utils.VIDEOS_DIR).free
        if free_bytes < MIN_RECORDING_FREE_BYTES:
            _set_last_error_locked(
                "insufficient_storage",
                f"Only {free_bytes} bytes are free; at least "
                f"{MIN_RECORDING_FREE_BYTES} bytes are required",
            )
            _persist_recording_state_locked()
            return {
                "status": "error",
                "error_code": "insufficient_storage",
                "details": last_recording_error["message"],
                "free_space_bytes": free_bytes,
                "required_free_bytes": MIN_RECORDING_FREE_BYTES,
            }
        selected_audio_device = None
        audio_file = None
        audio_command = None

        if system == "Linux":
            camera_device = _find_linux_camera_device()
            if camera_device is None:
                _remove_file(output_file)
                _set_last_error_locked(
                    "camera_unavailable",
                    "Camera capture device is not available",
                )
                _persist_recording_state_locked()
                return {
                    "status": "error",
                    "error_code": "camera_unavailable",
                    "details": last_recording_error["message"],
                }
            raw_file = f"{output_file}.mjpeg"
            if audio_enabled:
                selected_audio_device = audio.resolve_capture_device(
                    camera_settings.get("audio_device", "auto")
                )
                if selected_audio_device is None:
                    _remove_file(output_file)
                    _set_last_error_locked(
                        "audio_device_unavailable",
                        "Configured audio capture device is not available",
                    )
                    _persist_recording_state_locked()
                    return {
                        "status": "error",
                        "error_code": "audio_device_unavailable",
                        "details": last_recording_error["message"],
                    }
                audio_file = _build_audio_temp_file(output_file)
                audio_command = audio.build_arecord_command(
                    selected_audio_device["id"],
                )
            capture_command = _build_linux_capture_command(
                video_size,
                fps,
                raw_file,
                camera_device,
            )
            command = _build_linux_command(
                raw_file,
                fps,
                output_file,
                audio_file=audio_file,
            )
            capture_format = "ffmpeg_v4l2_mjpeg_raw"
        elif system == "Windows":
            camera_device = "video=AT025"
            raw_file = None
            capture_command = None
            command = _build_windows_command(video_size, fps, output_file)
            capture_format = "h264"
        else:
            _remove_file(output_file)
            _set_last_error_locked(
                "unsupported_os",
                f"Unsupported OS: {system}",
            )
            _persist_recording_state_locked()
            return {
                "status": "error",
                "error_code": "unsupported_os",
                "details": last_recording_error["message"],
            }

        recording_output_file = output_file
        recording_raw_file = raw_file
        recording_audio_file = audio_file
        recording_audio_device = selected_audio_device
        recording_audio_lead_seconds = 0.0
        recording_camera_device = camera_device
        recording_camera_control_state = None
        recording_video_size = video_size
        recording_fps = fps
        recording_capture_format = capture_format
        recording_started_at_monotonic = None
        recording_started_at_utc = _utc_now_iso()
        recording_phase = "starting"
        recording_generation += 1
        last_recording_error = None
        if capture_command:
            # Release the idle SD camera owner before ALSA/UVC startup. The
            # preview branch remains stopped until the first FullHD byte proves
            # that the primary recorder is healthy.
            _preview_call(
                "prepare_for_recording",
                camera_device,
                raw_file,
                float(fps),
            )
            recording_camera_control_state = _lock_recording_exposure(
                camera_device,
                video_size,
                fps,
            )
        _persist_recording_state_locked()

        try:
            ffmpeg_log_file = open(FFMPEG_LOG_FILE, "w", encoding="utf-8")
            ffmpeg_log_file.write(
                f"[INFO] Camera device: {camera_device}\n"
                f"[INFO] Capture: {video_size} @ {fps} fps, format={capture_format}\n"
                f"[INFO] Capture command: "
                f"{shlex.join(capture_command) if capture_command else 'none'}\n"
                f"[INFO] Audio enabled: {bool(audio_command)}\n"
                f"[INFO] Audio device: "
                f"{selected_audio_device['id'] if selected_audio_device else 'none'}\n"
                f"[INFO] Audio command: "
                f"{shlex.join(audio_command) if audio_command else 'none'}\n"
            )
            ffmpeg_log_file.flush()
            if capture_command:
                if audio_command:
                    audio_process, audio_started_at = _start_audio_capture(
                        audio_command,
                        ffmpeg_log_file,
                        audio_file,
                    )
                capture_process = subprocess.Popen(
                    capture_command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=ffmpeg_log_file,
                )
                capture_launched_at = time.monotonic()
                video_started_at = _wait_for_first_capture_byte(
                    raw_file,
                    capture_process,
                    capture_launched_at,
                    float(fps),
                )
                if audio_command:
                    recording_audio_lead_seconds = max(
                        0.0,
                        video_started_at - audio_started_at,
                    )
                    command = _build_linux_command(
                        raw_file,
                        fps,
                        output_file,
                        audio_file=audio_file,
                        audio_lead_seconds=recording_audio_lead_seconds,
                    )
                ffmpeg_log_file.write(
                    f"[INFO] Audio lead before video: "
                    f"{recording_audio_lead_seconds:.6f} seconds\n"
                    f"[INFO] Remux command: {shlex.join(command)}\n"
                )
                ffmpeg_log_file.flush()
                ffmpeg_process = None
                recording_raw_file = raw_file
                recording_audio_file = audio_file
                recording_audio_device = selected_audio_device
                recording_remux_command = command
            else:
                ffmpeg_process = subprocess.Popen(
                    command,
                    stdin=subprocess.PIPE,
                    stdout=ffmpeg_log_file,
                    stderr=ffmpeg_log_file,
                )
                video_started_at = time.monotonic()
        except audio.AudioError as error:
            _stop_capture_process(capture_process)
            _stop_capture_process(audio_process)
            _close_process_resources(ffmpeg_process)
            details = _log_tail()
            _clear_recording_state()
            _remove_file(output_file)
            _remove_file(raw_file)
            _remove_file(audio_file)
            _set_last_error_locked(
                error.code,
                details or error.details or str(error),
            )
            _persist_recording_state_locked()
            _preview_call("recording_finished")
            return {
                "status": "error",
                "error_code": error.code,
                "details": last_recording_error["message"],
            }
        except (OSError, subprocess.SubprocessError) as error:
            _stop_capture_process(capture_process)
            _stop_capture_process(audio_process)
            _close_process_resources(ffmpeg_process)
            _clear_recording_state()
            _remove_file(output_file)
            _remove_file(raw_file)
            _remove_file(audio_file)
            _set_last_error_locked("capture_start_failed", str(error))
            _persist_recording_state_locked()
            _preview_call("recording_finished")
            return {
                "status": "error",
                "error_code": "capture_start_failed",
                "details": str(error),
            }

        # _wait_for_first_capture_byte already covers the startup observation
        # window on Linux. Windows still needs the original stability delay.
        if not capture_command:
            time.sleep(FFMPEG_STARTUP_DELAY)
        capture_return_code = (
            capture_process.poll()
            if capture_process is not None
            else None
        )
        ffmpeg_return_code = (
            ffmpeg_process.poll()
            if ffmpeg_process is not None
            else None
        )
        audio_return_code = (
            audio_process.poll()
            if audio_process is not None
            else None
        )
        if (
            capture_return_code is not None
            or ffmpeg_return_code is not None
            or audio_return_code is not None
        ):
            _stop_capture_process(capture_process)
            _stop_capture_process(audio_process)
            _close_process_resources(ffmpeg_process)
            details = _log_tail()
            _clear_recording_state()
            _remove_file(output_file)
            _remove_file(raw_file)
            _remove_file(audio_file)
            if ffmpeg_return_code is not None:
                return_code = ffmpeg_return_code
            elif audio_return_code is not None:
                return_code = audio_return_code
            else:
                return_code = capture_return_code
            _set_last_error_locked(
                "capture_start_failed",
                details or f"Capture exited with code {return_code}",
            )
            _persist_recording_state_locked()
            _preview_call("recording_finished")
            return {
                "status": "error",
                "error_code": "capture_start_failed",
                "details": last_recording_error["message"],
            }

        recording_started_at_monotonic = video_started_at
        recording_started_at_utc = _utc_now_iso()
        recording_phase = "recording"
        _persist_recording_state_locked()
        _start_watchdog_locked()
        _preview_call("recording_started")

        return {
            "status": "recording_started",
            "file": output_file,
            "format": capture_format,
            "device": camera_device,
            "resolution": video_size,
            "fps": fps,
            "frame_rate_control": _public_camera_control_state(
                recording_camera_control_state
            ),
            "audio": {
                "enabled": bool(audio_command),
                "device": selected_audio_device,
                "codec": "aac" if audio_command else None,
                "sample_rate": audio.AUDIO_SAMPLE_RATE if audio_command else None,
                "channels": audio.AUDIO_CHANNELS if audio_command else None,
            },
            "storage_cleanup": storage_cleanup,
        }


def stop_recording():
    global recording_phase, recording_generation, last_recording_error

    with recording_lock:
        _restore_recording_state_locked()
        _refresh_recording_state_locked()
        if recording_phase == "finalizing":
            return {
                "status": "already_finalizing",
                "file": recording_output_file,
            }

        has_process = any(
            process is not None
            for process in (capture_process, ffmpeg_process, audio_process)
        )
        has_recovery_source = bool(
            recording_raw_file and os.path.isfile(recording_raw_file)
        )
        if not has_process and not has_recovery_source:
            return {"status": "no_recording_running"}

        process = ffmpeg_process
        capture = capture_process
        audio_capture = audio_process
        output_file = recording_output_file
        raw_file = recording_raw_file
        audio_file = recording_audio_file
        remux_command = recording_remux_command
        fps = float(recording_fps or camera_settings.get("fps", "30"))
        elapsed_seconds = (
            _recording_duration_seconds()
            if recording_started_at_monotonic is not None
            else 0.0
        )
        was_interrupted = recording_phase == "interrupted"
        previous_error = dict(last_recording_error) if last_recording_error else None
        recording_phase = "finalizing"
        recording_generation += 1
        _persist_recording_state_locked()

    capture_return_code = None
    audio_return_code = None
    return_code = None
    warning_parts = []
    quality = None
    audio_recovered = bool(audio_file)
    remux_timeout = _file_processing_timeout(
        raw_file,
        FFMPEG_REMUX_TIMEOUT,
        REMUX_MIN_THROUGHPUT_BYTES_PER_SECOND,
    )
    # Stop the disk-tail/scaler before stopping the primary capture. Idle SD
    # preview is restarted only after potentially expensive MP4 finalization.
    _preview_call("recording_stopped")

    if raw_file:
        capture_was_running = capture is not None and capture.poll() is None
        audio_was_running = (
            audio_capture is not None and audio_capture.poll() is None
        )
        capture_return_code = _stop_capture_process(capture)
        audio_return_code = _stop_capture_process(audio_capture)
        _release_recording_camera_controls()
        if capture is not None and not capture_was_running:
            warning_parts.append(
                f"Video capture ended before stop (code {capture_return_code})"
            )
            was_interrupted = True
        elif capture_return_code not in (None, 0, 255, -signal.SIGINT):
            warning_parts.append(
                f"Video capture exited with code {capture_return_code}"
            )
            was_interrupted = True

        audio_stopped_normally = (
            audio_return_code in (None, 0, -signal.SIGINT)
            or (audio_return_code == 1 and audio_was_running)
        )
        if audio_capture is not None and not audio_stopped_normally:
            warning_parts.append(
                f"Audio capture exited with code {audio_return_code}"
            )
            was_interrupted = True

        owned_log = None
        log_output = ffmpeg_log_file
        try:
            if log_output is None or log_output.closed:
                owned_log = open(FFMPEG_LOG_FILE, "a", encoding="utf-8")
                log_output = owned_log
            if not raw_file or not os.path.isfile(raw_file) or _safe_file_size(raw_file) == 0:
                raise OSError("Raw MJPEG recovery file is missing or empty")
            if remux_command is None:
                remux_command = _build_linux_command(
                    raw_file,
                    str(int(fps)),
                    output_file,
                    audio_file=audio_file if os.path.isfile(audio_file or "") else None,
                    audio_lead_seconds=recording_audio_lead_seconds,
                )
            remux = subprocess.run(
                remux_command,
                stdout=log_output,
                stderr=log_output,
                timeout=remux_timeout,
                check=False,
            )
            return_code = remux.returncode

            # A damaged/missing audio tail must not make an otherwise intact
            # video unrecoverable. Retry once with the raw MJPEG stream alone.
            if return_code != 0 and audio_file:
                warning_parts.append(
                    "Audio could not be finalized; recovered video without audio"
                )
                audio_recovered = False
                video_only_command = _build_linux_command(
                    raw_file,
                    str(int(fps)),
                    output_file,
                )
                retry = subprocess.run(
                    video_only_command,
                    stdout=log_output,
                    stderr=log_output,
                    timeout=remux_timeout,
                    check=False,
                )
                return_code = retry.returncode
        except subprocess.TimeoutExpired:
            warning_parts.append("FFmpeg remux timed out")
            return_code = 124
        except (OSError, subprocess.SubprocessError) as error:
            warning_parts.append(f"FFmpeg remux failed: {error}")
            return_code = 1
        finally:
            if owned_log is not None:
                owned_log.close()

        if return_code == 0:
            quality = _probe_recording(output_file, elapsed_seconds, fps)
            if not quality.get("valid"):
                warning_parts.append(
                    f"Output validation failed: {quality.get('error', 'invalid video')}"
                )
                return_code = 2
            elif not quality.get("healthy"):
                warning_parts.append(
                    "Frame delivery was below the FullHD 30 fps health threshold"
                )
    elif process is not None:
        return_code = process.poll()
        if return_code is None:
            try:
                process.stdin.write(b"q\n")
                process.stdin.flush()
                return_code = process.wait(timeout=FFMPEG_STOP_TIMEOUT)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                warning_parts.append(
                    "FFmpeg did not stop cleanly and was terminated"
                )
                try:
                    process.terminate()
                except ProcessLookupError:
                    return_code = process.poll()

            try:
                if return_code is None:
                    return_code = process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                return_code = process.wait(timeout=3)
        else:
            warning_parts.append(
                f"FFmpeg had already exited with code {return_code}"
            )

    with recording_lock:
        _close_process_resources(process)
        if return_code == 0:
            _remove_file(raw_file)
            _remove_file(audio_file)
            _clear_recording_state()
            if warning_parts:
                code = (
                    "recording_quality_degraded"
                    if quality and not quality.get("healthy")
                    else "recording_recovered_with_warning"
                )
                _set_last_error_locked(
                    code,
                    "; ".join(warning_parts),
                    recoverable=False,
                )
            elif not was_interrupted:
                # A fully healthy new recording clears errors from earlier runs.
                last_recording_error = None
            elif previous_error:
                last_recording_error = previous_error
                last_recording_error["recoverable"] = False
            _persist_recording_state_locked()
        elif raw_file and os.path.isfile(raw_file):
            recording_phase = "interrupted"
            _set_last_error_locked(
                "recording_finalization_failed",
                "; ".join(warning_parts) or f"FFmpeg exited with code {return_code}",
                recoverable=True,
            )
            _persist_recording_state_locked()
        else:
            _clear_recording_state()
            _set_last_error_locked(
                "recording_finalization_failed",
                "; ".join(warning_parts) or f"FFmpeg exited with code {return_code}",
                recoverable=False,
            )
            _persist_recording_state_locked()

    storage_cleanup = None
    if return_code == 0:
        try:
            protected = {os.path.basename(output_file)} if output_file else set()
            storage_cleanup = storage_manager.apply_policy(
                trigger="recording_stopped",
                protected_filenames=protected,
            )
        except OSError as error:
            warning_parts.append(f"Storage cleanup failed: {error}")

    response = {
        "status": "recording_stopped",
        "file": output_file,
        "returncode": return_code,
        "recovered": was_interrupted,
        "recoverable": return_code != 0 and bool(raw_file and os.path.isfile(raw_file)),
        "audio_recovered": audio_recovered,
    }
    if capture_return_code is not None:
        response["capture_returncode"] = capture_return_code
    if audio_return_code is not None:
        response["audio_capture_returncode"] = audio_return_code
    if quality is not None:
        response["quality"] = quality
    if warning_parts:
        response["warning"] = "; ".join(warning_parts)
    if storage_cleanup is not None:
        response["storage_cleanup"] = storage_cleanup
    _preview_call("recording_finished")
    return response


def get_settings():
    return dict(camera_settings)


def get_persisted_recording_status() -> dict:
    """Read cross-process recording state without invoking recovery logic."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(RECORDING_STATE_FILE, flags)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 64 * 1024:
                raise OSError("unsafe_recording_state")
            payload = json.loads(os.read(descriptor, 64 * 1024 + 1))
        finally:
            os.close(descriptor)
        if not isinstance(payload, dict):
            raise ValueError("invalid_recording_state")
        phase = payload.get("phase")
        if phase not in {
            "idle",
            "starting",
            "recording",
            "interrupted",
            "finalizing",
        }:
            raise ValueError("invalid_recording_phase")
    except FileNotFoundError:
        phase = "idle"
    except (OSError, ValueError, json.JSONDecodeError):
        # An unreadable state must block disruptive operations until the
        # backend can resolve it; treating it as idle would risk data loss.
        phase = "unknown"

    return {
        "status": "ok" if phase != "unknown" else "error",
        "state": phase,
        "recording": phase in {
            "starting",
            "recording",
            "interrupted",
            "finalizing",
            "unknown",
        },
        "capture_active": phase in {"starting", "recording", "unknown"},
        "interrupted": phase == "interrupted",
        "finalizing": phase == "finalizing",
    }


def get_preview_source() -> dict:
    """Return internal source data used by the authenticated preview route."""
    with recording_lock:
        _restore_recording_state_locked()
        active = recording_phase == "recording" and bool(recording_raw_file)
        device = recording_camera_device if active else None
        raw_file = recording_raw_file if active else None
        fps = float(recording_fps or camera_settings.get("fps", "30"))
    if not device:
        device = find_camera_device(timeout=0.0)
    return {
        "camera_device": device,
        "recording_raw_file": raw_file,
        "recording_fps": fps,
    }


def get_recording_status():
    with recording_lock:
        _restore_recording_state_locked()
        _refresh_recording_state_locked()
        video_process = ffmpeg_process or capture_process
        capture_active = (
            video_process is not None and video_process.poll() is None
        )
        audio_recording = (
            audio_process is not None and audio_process.poll() is None
        )
        recoverable = bool(
            recording_raw_file and os.path.isfile(recording_raw_file)
        )
        active_state = recording_phase in {
            "starting",
            "recording",
            "interrupted",
            "finalizing",
        }
        duration_seconds = _recording_duration_seconds() if active_state else 0.0
        raw_size = _safe_file_size(recording_raw_file)
        output_size = _safe_file_size(recording_output_file)
        audio_size = _safe_file_size(recording_audio_file)
        phase = recording_phase
        output_file = recording_output_file
        camera_device = recording_camera_device
        video_size = recording_video_size
        fps = recording_fps
        capture_format = recording_capture_format
        error = dict(last_recording_error) if last_recording_error else None
        audio_device = recording_audio_device
        camera_control_state = _public_camera_control_state(
            recording_camera_control_state
        )
        audio_lead = recording_audio_lead_seconds
        audio_enabled_for_recording = recording_audio_file is not None

    os.makedirs(utils.VIDEOS_DIR, exist_ok=True)
    disk = shutil.disk_usage(utils.VIDEOS_DIR)
    available_camera = None
    if platform.system() == "Linux":
        available_camera = _find_linux_camera_device(timeout=0.0)
    elif platform.system() == "Windows":
        available_camera = "video=AT025"

    return {
        "status": "ok",
        "state": phase,
        "recording": active_state,
        "capture_active": capture_active,
        "interrupted": phase == "interrupted",
        "finalizing": phase == "finalizing",
        "recoverable": recoverable,
        "file": output_file if active_state else None,
        "duration_seconds": round(duration_seconds, 3),
        "current_size_bytes": raw_size or output_size,
        "current_size_mb": round((raw_size or output_size) / (1024 * 1024), 2),
        "source_size_bytes": raw_size,
        "audio_size_bytes": audio_size,
        "free_space_bytes": disk.free,
        "free_space_gb": round(disk.free / (1024 ** 3), 2),
        "minimum_free_space_bytes": MIN_RECORDING_FREE_BYTES,
        "required_finalization_space_bytes": (
            raw_size + MIN_RECORDING_FREE_BYTES if active_state else 0
        ),
        "resolution": video_size,
        "fps": fps,
        "format": capture_format,
        "camera": {
            "available": available_camera is not None,
            "device": camera_device or available_camera,
            "frame_rate_control": camera_control_state,
        },
        "last_error": error,
        "audio": {
            "enabled": audio_enabled_for_recording or audio_size > 0,
            "recording": audio_recording,
            "device": audio_device,
            "lead_seconds": round(audio_lead, 6),
        },
    }


def update_settings(
    resolution: str = None,
    fps: str = None,
    audio_enabled: bool = None,
    audio_device: str = None,
):
    if resolution:
        resolution = LEGACY_RESOLUTION_MAP.get(resolution, resolution)
        if resolution not in SUPPORTED_RESOLUTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported resolution preset: {resolution}",
            )
        camera_settings["resolution"] = resolution

    if fps:
        fps = str(fps)
        fps = LEGACY_FPS_MAP.get(fps, fps)
        if fps not in SUPPORTED_FPS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported fps preset: {fps}",
            )
        camera_settings["fps"] = fps

    if audio_enabled is not None:
        camera_settings["audio_enabled"] = bool(audio_enabled)

    if audio_device is not None:
        audio_device = audio_device.strip() or "auto"
        available_ids = {item["id"] for item in audio.list_capture_devices()}
        if audio_device != "auto" and audio_device not in available_ids:
            raise HTTPException(
                status_code=400,
                detail="Unsupported audio capture device",
            )
        camera_settings["audio_device"] = audio_device

    with open(SETTINGS_FILE, "w", encoding="utf-8") as settings_file:
        json.dump(camera_settings, settings_file)

    return dict(camera_settings)
