'''app/camera.py'''
from fastapi import HTTPException
import subprocess
import platform
import os
import time
from app import utils
import json

ffmpeg_process = None

SETTINGS_FILE = "camera_settings.json"

camera_settings = {
    "resolution": "FHD",
    "fps": "30"
}

SUPPORTED_RESOLUTIONS = {
    "SD": "640x360",
    "HD": "1280x720",
    "FHD": "1920x1080",
    "2K": "2688x1512",
    "4K": "3840x2160",
}

LEGACY_RESOLUTION_MAP = {
    value: key for key, value in SUPPORTED_RESOLUTIONS.items()
}

SUPPORTED_FPS = {"30", "60"}

BITRATE_PRESETS = {
    ("SD", "30"): ("2M", "4M"),
    ("SD", "60"): ("3M", "6M"),
    ("HD", "30"): ("4M", "8M"),
    ("HD", "60"): ("6M", "12M"),
    ("FHD", "30"): ("8M", "16M"),
    ("FHD", "60"): ("12M", "24M"),
}

_linux_encoder_cache = None


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


def _bitrate_profile(resolution_key: str, fps: str):
    return BITRATE_PRESETS.get((resolution_key, fps), ("8M", "16M"))


def _probe_ffmpeg_encoders():
    try:
        return subprocess.check_output(
            ["ffmpeg", "-hide_banner", "-encoders"],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        print(f"[WARN] Failed to probe ffmpeg encoders: {e}")
        return ""


def _probe_ffmpeg_version():
    try:
        return subprocess.check_output(
            ["ffmpeg", "-version"],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except Exception as e:
        print(f"[WARN] Failed to probe ffmpeg version: {e}")
        return ""


def _linux_encoder_candidates():
    global _linux_encoder_cache

    encoders_output = _probe_ffmpeg_encoders()
    version_output = _probe_ffmpeg_version()

    candidates = []
    if _linux_encoder_cache:
        candidates.append(_linux_encoder_cache)

    if "h264_rkmpp" in encoders_output or "--enable-rkmpp" in version_output:
        candidates.append("h264_rkmpp")

    if "h264_v4l2m2m" in encoders_output:
        candidates.append("h264_v4l2m2m")

    candidates.append("libx264")

    deduped_candidates = []
    for encoder_name in candidates:
        if encoder_name not in deduped_candidates:
            deduped_candidates.append(encoder_name)

    return deduped_candidates


def _linux_encoder_args(encoder_name: str, bitrate: str, bufsize: str):
    if encoder_name == "h264_rkmpp":
        return [
            "-vf", "format=nv12",
            "-c:v", "h264_rkmpp",
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-bufsize", bufsize,
            "-g", "60",
        ]

    if encoder_name == "h264_v4l2m2m":
        return [
            "-vf", "format=nv12",
            "-c:v", "h264_v4l2m2m",
            "-b:v", bitrate,
            "-maxrate", bitrate,
            "-bufsize", bufsize,
            "-g", "60",
        ]

    return [
        "-c:v", "libx264",
        "-preset", "ultrafast",
        "-tune", "zerolatency",
        "-crf", "30",
        "-pix_fmt", "yuv420p",
        "-g", "60",
    ]


def _build_linux_command(video_size: str, fps: str, output_file: str,
                         encoder_name: str, bitrate: str, bufsize: str):
    return [
        "ffmpeg",
        "-y",
        "-thread_queue_size", "1024",
        "-f", "v4l2",
        "-input_format", "mjpeg",
        "-framerate", fps,
        "-video_size", video_size,
        "-i", "/dev/video0",
        *_linux_encoder_args(encoder_name, bitrate, bufsize),
        "-movflags", "+faststart",
        output_file
    ]


def _start_linux_ffmpeg(video_size: str, fps: str, output_file: str,
                        bitrate: str, bufsize: str):
    global _linux_encoder_cache

    attempted_encoders = []
    log_file = open("ffmpeg.log", "w")

    for encoder_name in _linux_encoder_candidates():
        attempted_encoders.append(encoder_name)
        command = _build_linux_command(
            video_size,
            fps,
            output_file,
            encoder_name,
            bitrate,
            bufsize,
        )

        log_file.write(f"[INFO] Trying encoder: {encoder_name}\n")
        log_file.flush()
        print(f"[INFO] Trying encoder: {encoder_name}")

        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=log_file,
            stderr=log_file,
        )

        time.sleep(1.5)
        if process.poll() is None:
            _linux_encoder_cache = encoder_name
            return process, encoder_name

        log_file.write(
            f"[WARN] Encoder {encoder_name} exited immediately with code {process.returncode}\n"
        )
        log_file.flush()
        print(
            f"[WARN] Encoder {encoder_name} exited immediately with code {process.returncode}"
        )

    log_file.close()
    return None, attempted_encoders



if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, "r") as f:
            camera_settings.update(_normalize_settings(json.load(f)))
    except Exception:
        pass

# -------------------
# 📼 Запись
# -------------------
def start_recording():
    global ffmpeg_process

    if ffmpeg_process is not None:
        return {"status": "already_recording"}
    
    resolution_key = camera_settings.get("resolution", "FHD")
    video_size = SUPPORTED_RESOLUTIONS.get(resolution_key)
    fps = camera_settings["fps"]
    bitrate, bufsize = _bitrate_profile(resolution_key, fps)

    if not video_size:
        # fallback + лог
        print(f"[WARN] Unsupported resolution preset: {resolution_key}, fallback to FHD")
        resolution_key = "FHD"
        video_size = SUPPORTED_RESOLUTIONS["FHD"]
        bitrate, bufsize = _bitrate_profile(resolution_key, fps)

    system = platform.system()
    output_file = utils.get_output_filename()

    try:
        if system == "Windows":
            command = [
                "ffmpeg",
                "-y",
                "-f", "dshow",
                "-framerate", camera_settings["fps"],
                "-video_size", video_size,
                "-vcodec", "mjpeg",
                "-i", "video=AT025",
                "-c:v", "libx264",
                "-preset", "ultrafast",
                "-crf", "23",
                "-pix_fmt", "yuv420p",
                output_file
            ]
            log_file = open("ffmpeg.log", "w")
            ffmpeg_process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=log_file,
                stderr=log_file,
            )
            encoder_name = "libx264"

        elif system == "Linux":
            ffmpeg_process, encoder_result = _start_linux_ffmpeg(
                video_size,
                fps,
                output_file,
                bitrate,
                bufsize,
            )
            if ffmpeg_process is None:
                return {
                    "status": "error",
                    "details": (
                        "Failed to start ffmpeg with encoders: "
                        + ", ".join(encoder_result)
                    ),
                }
            encoder_name = encoder_result

        else:
            return {"status": f"Unsupported OS: {system}"}

        return {
            "status": "recording_started",
            "file": output_file,
            "encoder": encoder_name,
            "bitrate": bitrate if system == "Linux" else None,
        }
    except Exception as e:
        ffmpeg_process = None
        return {"status": "error", "details": str(e)}

def stop_recording():
    global ffmpeg_process

    if ffmpeg_process:
        if ffmpeg_process.poll() is None:
            try:
                ffmpeg_process.stdin.write(b"q\n")
                ffmpeg_process.stdin.flush()
                ffmpeg_process.wait(timeout=5)
            except Exception as e:
                print(f"Ошибка при остановке FFmpeg: {e}")
                ffmpeg_process.kill()
        ffmpeg_process = None
        return {"status": "recording_stopped"}
    else:
        return {"status": "no_recording_running"}

def get_settings():
    return camera_settings

def update_settings(resolution: str = None, fps: str = None):
    if resolution:
        resolution = LEGACY_RESOLUTION_MAP.get(resolution, resolution)
        if resolution not in SUPPORTED_RESOLUTIONS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported resolution preset: {resolution}"
            )
        camera_settings["resolution"] = resolution

    if fps:
        fps = str(fps)
        if fps not in SUPPORTED_FPS:
            raise HTTPException(
                status_code=400,
                detail=f"Unsupported fps preset: {fps}"
            )
        camera_settings["fps"] = fps

    with open(SETTINGS_FILE, "w") as f:
        json.dump(camera_settings, f)

    return camera_settings
