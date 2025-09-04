import subprocess
import platform
import os
from datetime import datetime
import subprocess
from fastapi.responses import StreamingResponse
from PIL import Image
import io
import time
from app import utils

ffmpeg_process = None

# Настройки по умолчанию (можно менять через API)
camera_settings = {
    "resolution": "1280x720",
    "fps": "30"
}


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

def list_videos():
    os.makedirs("videos", exist_ok=True)
    files = sorted(os.listdir("videos"))
    return files

def generate_frames(resolution="1280x720", fps="30"):
    system = platform.system()
    if system == "Windows":
        command = [
            "ffmpeg",
            "-f", "dshow",
            "-i", "video=AT025",
            "-video_size", resolution,
            "-framerate", fps,
            "-f", "mjpeg",
            "-q:v", "5",
            "-"
        ]
    else:
        command = [
            "ffmpeg",
            "-f", "v4l2",
            "-framerate", fps,
            "-video_size", resolution,
            "-i", "/dev/video0",
            "-f", "mjpeg",
            "-q:v", "5",
            "-"
        ]

    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL
    )

    try:
        while True:
            frame = process.stdout.read(1024)
            if not frame:
                break
            yield (
                b"--frame\r\n"
                b"Content-Type: image/jpeg\r\n\r\n" +
                frame + b"\r\n"
            )
    finally:
        process.kill()

def generate_hls_stream():
    command = [
        "ffmpeg",
        "-f", "v4l2",
        "-i", "/dev/video0",
        "-s", "640x480",
        "-c:v", "libx264",
        "-f", "hls",
        "-hls_time", "2",
        "-hls_list_size", "3",
        "-hls_flags", "delete_segments",
        "stream.m3u8"
    ]
    subprocess.Popen(command)
    return "stream.m3u8"


def fake_stream():
    img = Image.new("RGB", (640, 480), color=(73, 109, 137))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    frame = buf.getvalue()

    while True:
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        time.sleep(0.1)  # имитируем FPS

def get_settings():
    return camera_settings

def update_settings(resolution: str = None, fps: str = None):
    if resolution:
        camera_settings["resolution"] = resolution
    if fps:
        camera_settings["fps"] = fps
    return camera_settings