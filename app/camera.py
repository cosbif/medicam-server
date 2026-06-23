'''Camera settings and video recording lifecycle.'''

from fastapi import HTTPException
import glob
import json
import os
import platform
import shlex
import stat
import subprocess
import threading
import time

from app import utils


SETTINGS_FILE = "camera_settings.json"
FFMPEG_LOG_FILE = "ffmpeg.log"
CAMERA_DISCOVERY_TIMEOUT = 3.0
FFMPEG_STARTUP_DELAY = 1.0
FFMPEG_STOP_TIMEOUT = 10.0

camera_settings = {
    "resolution": "FHD",
    "fps": "30",
}

# FullHD is intentionally the maximum supported resolution. The camera exposes
# FullHD MJPEG at 30/60 fps, while uncompressed YUYV is limited to 5 fps.
SUPPORTED_RESOLUTIONS = {
    "SD": "640x360",
    "HD": "1280x720",
    "FHD": "1920x1080",
}

LEGACY_RESOLUTION_MAP = {
    value: key for key, value in SUPPORTED_RESOLUTIONS.items()
}

SUPPORTED_FPS = {"30", "60"}

ffmpeg_process = None
ffmpeg_log_file = None
recording_output_file = None
recording_lock = threading.Lock()


def _normalize_settings(settings: dict | None):
    settings = settings or {}

    resolution = str(settings.get("resolution", camera_settings["resolution"]))
    resolution = LEGACY_RESOLUTION_MAP.get(resolution, resolution)
    if resolution not in SUPPORTED_RESOLUTIONS:
        resolution = "FHD"

    fps = str(settings.get("fps", camera_settings["fps"]))
    if fps not in SUPPORTED_FPS:
        fps = "30"

    return {
        "resolution": resolution,
        "fps": fps,
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


def _build_linux_command(video_size: str, fps: str, output_file: str,
                         camera_device: str):
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-thread_queue_size", "512",
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-framerate", fps,
        "-video_size", video_size,
        "-i", camera_device,
        "-map", "0:v:0",
        "-an",
        # The UVC camera already produces compressed MJPEG. Stream copy avoids
        # decoding, colorspace conversion and re-encoding, which could process
        # FullHD at only ~0.43x realtime on the Radxa.
        "-c:v", "copy",
        "-movflags", "+faststart",
        output_file,
    ]


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
    global ffmpeg_process, recording_output_file

    ffmpeg_process = None
    recording_output_file = None


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


if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            camera_settings.update(_normalize_settings(json.load(settings_file)))
    except (OSError, json.JSONDecodeError, TypeError):
        pass


def start_recording():
    global ffmpeg_process, ffmpeg_log_file, recording_output_file

    with recording_lock:
        if ffmpeg_process is not None:
            if ffmpeg_process.poll() is None:
                return {
                    "status": "already_recording",
                    "file": recording_output_file,
                }

            # A disconnected camera can terminate FFmpeg between API calls.
            # Reap that process and allow the next /start request to recover.
            _close_process_resources(ffmpeg_process)
            _clear_recording_state()

        resolution_key = camera_settings.get("resolution", "FHD")
        video_size = SUPPORTED_RESOLUTIONS.get(resolution_key)
        fps = str(camera_settings.get("fps", "30"))
        if not video_size or fps not in SUPPORTED_FPS:
            normalized = _normalize_settings(camera_settings)
            camera_settings.update(normalized)
            resolution_key = normalized["resolution"]
            video_size = SUPPORTED_RESOLUTIONS[resolution_key]
            fps = normalized["fps"]

        system = platform.system()
        output_file = utils.get_output_filename()

        if system == "Linux":
            camera_device = _find_linux_camera_device()
            if camera_device is None:
                _remove_file(output_file)
                return {
                    "status": "error",
                    "details": "Camera capture device is not available",
                }
            command = _build_linux_command(
                video_size,
                fps,
                output_file,
                camera_device,
            )
            capture_format = "mjpeg_copy"
        elif system == "Windows":
            camera_device = "video=AT025"
            command = _build_windows_command(video_size, fps, output_file)
            capture_format = "h264"
        else:
            _remove_file(output_file)
            return {"status": "error", "details": f"Unsupported OS: {system}"}

        try:
            ffmpeg_log_file = open(FFMPEG_LOG_FILE, "w", encoding="utf-8")
            ffmpeg_log_file.write(
                f"[INFO] Camera device: {camera_device}\n"
                f"[INFO] Capture: {video_size} @ {fps} fps, format={capture_format}\n"
                f"[INFO] Command: {shlex.join(command)}\n"
            )
            ffmpeg_log_file.flush()
            ffmpeg_process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=ffmpeg_log_file,
                stderr=ffmpeg_log_file,
            )
            recording_output_file = output_file
        except (OSError, subprocess.SubprocessError) as error:
            _close_process_resources(ffmpeg_process)
            _clear_recording_state()
            _remove_file(output_file)
            return {"status": "error", "details": str(error)}

        time.sleep(FFMPEG_STARTUP_DELAY)
        return_code = ffmpeg_process.poll()
        if return_code is not None:
            _close_process_resources(ffmpeg_process)
            details = _log_tail()
            _clear_recording_state()
            _remove_file(output_file)
            return {
                "status": "error",
                "details": details or f"FFmpeg exited with code {return_code}",
            }

        return {
            "status": "recording_started",
            "file": output_file,
            "format": capture_format,
            "device": camera_device,
            "resolution": video_size,
            "fps": fps,
        }


def stop_recording():
    global ffmpeg_process

    with recording_lock:
        if ffmpeg_process is None:
            return {"status": "no_recording_running"}

        process = ffmpeg_process
        output_file = recording_output_file
        return_code = process.poll()
        warning = None

        if return_code is None:
            try:
                process.stdin.write(b"q\n")
                process.stdin.flush()
                return_code = process.wait(timeout=FFMPEG_STOP_TIMEOUT)
            except (BrokenPipeError, OSError, subprocess.TimeoutExpired):
                warning = "FFmpeg did not stop cleanly and was terminated"
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
            warning = f"FFmpeg had already exited with code {return_code}"

        _close_process_resources(process)
        _clear_recording_state()

        response = {
            "status": "recording_stopped",
            "file": output_file,
            "returncode": return_code,
        }
        if warning:
            response["warning"] = warning
        return response


def get_settings():
    return camera_settings


def update_settings(resolution: str = None, fps: str = None):
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
        if fps not in SUPPORTED_FPS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported fps preset: {fps}",
            )
        camera_settings["fps"] = fps

    with open(SETTINGS_FILE, "w", encoding="utf-8") as settings_file:
        json.dump(camera_settings, settings_file)

    return camera_settings
