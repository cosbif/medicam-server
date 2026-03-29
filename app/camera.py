'''app/camera.py'''
from fastapi import HTTPException
import subprocess
import platform
import os
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

    if not video_size:
        # fallback + лог
        print(f"[WARN] Unsupported resolution preset: {resolution_key}, fallback to FHD")
        video_size = SUPPORTED_RESOLUTIONS["FHD"]

    system = platform.system()
    output_file = utils.get_output_filename()

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

    elif system == "Linux":
        command = [
            "ffmpeg",
            "-y",
    
            "-f", "v4l2",
            "-input_format", "mjpeg",
            "-framerate", camera_settings["fps"],
            "-video_size", video_size,
            "-i", "/dev/video0",

            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "28",
            "-pix_fmt", "yuv420p",

            "-movflags", "+faststart",
            output_file
        ]

    else:
        return {"status": f"Unsupported OS: {system}"}

    try:
        log_file = open("ffmpeg.log", "w")
        ffmpeg_process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=log_file, stderr=log_file)
        return {"status": "recording_started", "file": output_file}
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
