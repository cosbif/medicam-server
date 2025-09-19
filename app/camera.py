import subprocess
import platform
import os
from app import utils
import json

ffmpeg_process = None

SETTINGS_FILE = "camera_settings.json"

camera_settings = {
    "resolution": "1920x1080",
    "fps": "30"
}

if os.path.exists(SETTINGS_FILE):
    try:
        with open(SETTINGS_FILE, "r") as f:
            camera_settings.update(json.load(f))
    except Exception:
        pass

# -------------------
# 📼 Запись
# -------------------
def start_recording():
    global ffmpeg_process

    if ffmpeg_process is not None:
        return {"status": "already_recording"}

    system = platform.system()
    output_file = utils.get_output_filename()

    if system == "Windows":
        command = [
            "ffmpeg",
            "-y",
            "-f", "dshow",
            "-framerate", camera_settings["fps"],
            "-video_size", camera_settings["resolution"],
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
            "-framerate", camera_settings["fps"],
            "-video_size", camera_settings["resolution"],
            "-input_format", "mjpeg",
            "-i", "/dev/video0",
            "-c:v", "libx264",
            "-preset", "ultrafast",
            "-crf", "23",
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
        camera_settings["resolution"] = resolution
    if fps:
        camera_settings["fps"] = fps
    with open(SETTINGS_FILE, "w") as f:
        json.dump(camera_settings, f)
    return camera_settings