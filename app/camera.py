'''Camera settings and video recording lifecycle.'''

from fastapi import HTTPException
import glob
import json
import os
import platform
import shlex
import signal
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
LINUX_V4L2_BUFFER_COUNT = "8"

capture_process = None
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
    fps = LEGACY_FPS_MAP.get(fps, fps)
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


def _split_video_size(video_size: str):
    width, height = video_size.split("x", maxsplit=1)
    return width, height


def _build_linux_capture_command(video_size: str, fps: str, camera_device: str):
    width, height = _split_video_size(video_size)

    return [
        "v4l2-ctl",
        "--silent",
        "-d", camera_device,
        f"--set-fmt-video=width={width},height={height},pixelformat=MJPG",
        f"--set-parm={fps}",
        f"--stream-mmap={LINUX_V4L2_BUFFER_COUNT}",
        "--stream-to=-",
    ]


def _build_linux_command(video_size: str, fps: str, output_file: str,
                         camera_device: str):
    return [
        "ffmpeg",
        "-hide_banner",
        "-loglevel", "warning",
        "-nostats",
        "-y",
        "-f", "mjpeg",
        "-framerate", fps,
        "-i", "pipe:0",
        "-map", "0:v:0",
        "-an",
        # The UVC camera already produces compressed MJPEG. FFmpeg only muxes
        # the v4l2-ctl pipe into MP4; it does not decode or re-encode frames.
        "-c:v", "copy",
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
    global capture_process, ffmpeg_process, recording_output_file

    capture_process = None
    ffmpeg_process = None
    recording_output_file = None


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


if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as settings_file:
            camera_settings.update(_normalize_settings(json.load(settings_file)))
    except (OSError, json.JSONDecodeError, TypeError):
        pass


def start_recording():
    global capture_process, ffmpeg_process, ffmpeg_log_file, recording_output_file

    with recording_lock:
        if ffmpeg_process is not None:
            if ffmpeg_process.poll() is None:
                return {
                    "status": "already_recording",
                    "file": recording_output_file,
                }

            # A disconnected camera can terminate FFmpeg between API calls.
            # Reap that process and allow the next /start request to recover.
            _stop_capture_process(capture_process)
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
            capture_command = _build_linux_capture_command(
                video_size,
                fps,
                camera_device,
            )
            command = _build_linux_command(
                video_size,
                fps,
                output_file,
                camera_device,
            )
            capture_format = "v4l2_mjpeg_pipe"
        elif system == "Windows":
            camera_device = "video=AT025"
            capture_command = None
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
                f"[INFO] Capture command: "
                f"{shlex.join(capture_command) if capture_command else 'none'}\n"
                f"[INFO] Command: {shlex.join(command)}\n"
            )
            ffmpeg_log_file.flush()
            if capture_command:
                capture_process = subprocess.Popen(
                    capture_command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                ffmpeg_stdin = capture_process.stdout
            else:
                ffmpeg_stdin = subprocess.PIPE

            ffmpeg_process = subprocess.Popen(
                command,
                stdin=ffmpeg_stdin,
                stdout=ffmpeg_log_file,
                stderr=ffmpeg_log_file,
            )
            if capture_process is not None and capture_process.stdout is not None:
                capture_process.stdout.close()
            recording_output_file = output_file
        except (OSError, subprocess.SubprocessError) as error:
            _stop_capture_process(capture_process)
            _close_process_resources(ffmpeg_process)
            _clear_recording_state()
            _remove_file(output_file)
            return {"status": "error", "details": str(error)}

        time.sleep(FFMPEG_STARTUP_DELAY)
        capture_return_code = (
            capture_process.poll()
            if capture_process is not None
            else None
        )
        ffmpeg_return_code = ffmpeg_process.poll()
        if capture_return_code is not None or ffmpeg_return_code is not None:
            _stop_capture_process(capture_process)
            _close_process_resources(ffmpeg_process)
            details = _log_tail()
            _clear_recording_state()
            _remove_file(output_file)
            return_code = (
                ffmpeg_return_code
                if ffmpeg_return_code is not None
                else capture_return_code
            )
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
    global capture_process, ffmpeg_process

    with recording_lock:
        if ffmpeg_process is None:
            return {"status": "no_recording_running"}

        process = ffmpeg_process
        capture = capture_process
        output_file = recording_output_file
        return_code = process.poll()
        capture_return_code = None
        warning = None

        if return_code is None:
            if capture is not None:
                capture_return_code = _stop_capture_process(capture)
                try:
                    return_code = process.wait(timeout=FFMPEG_STOP_TIMEOUT)
                except subprocess.TimeoutExpired:
                    warning = "FFmpeg did not stop cleanly and was terminated"
                    process.terminate()
            else:
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
            capture_return_code = _stop_capture_process(capture)

        _close_process_resources(process)
        _clear_recording_state()

        response = {
            "status": "recording_stopped",
            "file": output_file,
            "returncode": return_code,
        }
        if capture_return_code is not None:
            response["capture_returncode"] = capture_return_code
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
        fps = LEGACY_FPS_MAP.get(fps, fps)
        if fps not in SUPPORTED_FPS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported fps preset: {fps}",
            )
        camera_settings["fps"] = fps

    with open(SETTINGS_FILE, "w", encoding="utf-8") as settings_file:
        json.dump(camera_settings, settings_file)

    return camera_settings
