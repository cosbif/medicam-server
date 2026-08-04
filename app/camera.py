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

from app import audio, utils


SETTINGS_FILE = "camera_settings.json"
FFMPEG_LOG_FILE = "ffmpeg.log"
CAMERA_DISCOVERY_TIMEOUT = 3.0
FFMPEG_STARTUP_DELAY = 1.0
FFMPEG_STOP_TIMEOUT = 10.0
FFMPEG_REMUX_TIMEOUT = 180.0
AUDIO_OPEN_ATTEMPTS = 8
AUDIO_OPEN_PROBE_DELAY = 0.15
AUDIO_OPEN_RETRY_DELAY = 0.35
AUDIO_TEMP_DIR = os.environ.get("MEDICAM_AUDIO_TEMP_DIR", "/dev/shm")

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
LINUX_V4L2_BUFFER_COUNT = "8"

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


def _split_video_size(video_size: str):
    width, height = video_size.split("x", maxsplit=1)
    return width, height


def _build_audio_temp_file(output_file: str):
    # PCM is tiny compared with FullHD MJPEG but frequent synchronous writes to
    # the same microSD can starve ALSA for several seconds. Linux tmpfs keeps
    # audio capture independent from video I/O. Fall back to the video folder
    # on systems without a writable /dev/shm.
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
    width, height = _split_video_size(video_size)

    return [
        "v4l2-ctl",
        "--silent",
        "-d", camera_device,
        f"--set-fmt-video=width={width},height={height},pixelformat=MJPG",
        f"--set-parm={fps}",
        f"--stream-mmap={LINUX_V4L2_BUFFER_COUNT}",
        f"--stream-to={raw_file}",
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
        # the raw v4l2-ctl capture into MP4; it does not decode or re-encode.
        "-c:v", "copy",
    ])
    if audio_file:
        command.extend([
            "-map", "1:a:0",
            "-c:a", "aac",
            "-b:a", audio.AUDIO_BITRATE,
            "-ar", str(audio.AUDIO_SAMPLE_RATE),
            "-ac", str(audio.AUDIO_CHANNELS),
            "-af", "aresample=async=1:first_pts=0",
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
    """Open ALSA before UVC, tolerating short desktop audio probes."""
    last_return_code = None
    for attempt in range(1, AUDIO_OPEN_ATTEMPTS + 1):
        with open(audio_file, "wb") as audio_output:
            process = subprocess.Popen(
                command,
                stdout=audio_output,
                stderr=log_file,
            )
        started_at = time.monotonic()
        time.sleep(AUDIO_OPEN_PROBE_DELAY)
        last_return_code = process.poll()
        if last_return_code is None:
            return process, started_at

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
    global recording_remux_command

    capture_process = None
    audio_process = None
    ffmpeg_process = None
    recording_output_file = None
    recording_raw_file = None
    recording_audio_file = None
    recording_audio_device = None
    recording_audio_lead_seconds = 0.0
    recording_remux_command = None


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
    global capture_process, audio_process, ffmpeg_process, ffmpeg_log_file
    global recording_output_file, recording_raw_file, recording_audio_file
    global recording_audio_device, recording_audio_lead_seconds
    global recording_remux_command

    with recording_lock:
        processes = [
            process
            for process in (ffmpeg_process, capture_process, audio_process)
            if process is not None
        ]
        if processes:
            if any(process.poll() is None for process in processes):
                return {
                    "status": "already_recording",
                    "file": recording_output_file,
                }
            # Preserve interrupted source files until /stop remuxes them. This
            # can recover everything captured before a USB disconnect.
            return {
                "status": "recording_interrupted",
                "file": recording_output_file,
                "details": "Finalize the interrupted recording with /stop",
            }

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

        system = platform.system()
        output_file = utils.get_output_filename()
        selected_audio_device = None
        audio_file = None
        audio_command = None

        if system == "Linux":
            camera_device = _find_linux_camera_device()
            if camera_device is None:
                _remove_file(output_file)
                return {
                    "status": "error",
                    "details": "Camera capture device is not available",
                }
            raw_file = f"{output_file}.mjpeg"
            if audio_enabled:
                selected_audio_device = audio.resolve_capture_device(
                    camera_settings.get("audio_device", "auto")
                )
                if selected_audio_device is None:
                    _remove_file(output_file)
                    return {
                        "status": "error",
                        "error_code": "audio_device_unavailable",
                        "details": "Configured audio capture device is not available",
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
            capture_format = "v4l2_mjpeg_raw"
        elif system == "Windows":
            camera_device = "video=AT025"
            raw_file = None
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
                    stdout=subprocess.DEVNULL,
                    stderr=ffmpeg_log_file,
                )
                video_started_at = time.monotonic()
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
            recording_output_file = output_file
        except audio.AudioError as error:
            _stop_capture_process(capture_process)
            _stop_capture_process(audio_process)
            _close_process_resources(ffmpeg_process)
            details = _log_tail()
            _clear_recording_state()
            _remove_file(output_file)
            _remove_file(raw_file)
            _remove_file(audio_file)
            return {
                "status": "error",
                "error_code": error.code,
                "details": details or error.details or str(error),
            }
        except (OSError, subprocess.SubprocessError) as error:
            _stop_capture_process(capture_process)
            _stop_capture_process(audio_process)
            _close_process_resources(ffmpeg_process)
            _clear_recording_state()
            _remove_file(output_file)
            _remove_file(raw_file)
            _remove_file(audio_file)
            return {"status": "error", "details": str(error)}

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
            "audio": {
                "enabled": bool(audio_command),
                "device": selected_audio_device,
                "codec": "aac" if audio_command else None,
                "sample_rate": audio.AUDIO_SAMPLE_RATE if audio_command else None,
                "channels": audio.AUDIO_CHANNELS if audio_command else None,
            },
        }


def stop_recording():
    global capture_process, audio_process, ffmpeg_process

    with recording_lock:
        if capture_process is None and ffmpeg_process is None and audio_process is None:
            return {"status": "no_recording_running"}

        process = ffmpeg_process
        capture = capture_process
        audio_capture = audio_process
        output_file = recording_output_file
        capture_return_code = None
        audio_return_code = None
        return_code = None
        warning = None

        if capture is not None:
            capture_was_running = capture.poll() is None
            audio_was_running = (
                audio_capture is not None and audio_capture.poll() is None
            )
            capture_return_code = _stop_capture_process(capture)
            audio_return_code = _stop_capture_process(audio_capture)
            if not capture_was_running:
                warning = (
                    "Video capture ended before stop "
                    f"(code {capture_return_code})"
                )
            elif capture_return_code not in (None, 0, -signal.SIGINT):
                warning = f"Video capture exited with code {capture_return_code}"
            # arecord returns 1 after handling SIGINT on this platform. When it
            # stayed alive throughout recording, this is a normal requested
            # stop rather than a capture failure.
            audio_stopped_normally = (
                audio_return_code in (None, 0, -signal.SIGINT)
                or (audio_return_code == 1 and audio_was_running)
            )
            if not audio_stopped_normally:
                audio_warning = f"Audio capture exited with code {audio_return_code}"
                warning = f"{warning}; {audio_warning}" if warning else audio_warning
            try:
                remux = subprocess.run(
                    recording_remux_command,
                    stdout=ffmpeg_log_file,
                    stderr=ffmpeg_log_file,
                    timeout=FFMPEG_REMUX_TIMEOUT,
                    check=False,
                )
                return_code = remux.returncode
            except (OSError, subprocess.SubprocessError) as error:
                warning = f"FFmpeg remux failed: {error}"
                return_code = 1
            except subprocess.TimeoutExpired:
                warning = "FFmpeg remux timed out"
                return_code = 124

            if return_code == 0:
                _remove_file(recording_raw_file)
                _remove_file(recording_audio_file)
        else:
            return_code = process.poll()
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
        if capture_return_code is not None:
            response["capture_returncode"] = capture_return_code
        if audio_return_code is not None:
            response["audio_capture_returncode"] = audio_return_code
        if warning:
            response["warning"] = warning
        return response


def get_settings():
    return dict(camera_settings)


def get_recording_status():
    video_process = ffmpeg_process or capture_process
    capture_active = video_process is not None and video_process.poll() is None
    audio_recording = audio_process is not None and audio_process.poll() is None
    pending_finalization = (
        recording_output_file is not None
        and any(
            process is not None
            for process in (ffmpeg_process, capture_process, audio_process)
        )
    )
    return {
        "recording": pending_finalization,
        "capture_active": capture_active,
        "interrupted": pending_finalization and not capture_active,
        "file": recording_output_file if pending_finalization else None,
        "audio": {
            "enabled": recording_audio_file is not None,
            "recording": audio_recording,
            "device": recording_audio_device,
            "lead_seconds": round(recording_audio_lead_seconds, 6),
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
