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
            "-framerate", "30",
            "-video_size", "1280x720",
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
            "-framerate", "30",
            "-video_size", "1280x720",
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
    if ffmpeg_process is None:
        return {"status": "not_recording"}

    try:
        ffmpeg_process.stdin.write(b"q")
        ffmpeg_process.stdin.flush()
    except Exception:
        ffmpeg_process.terminate()

    ffmpeg_process = None
    return {"status": "recording_stopped"}

def list_videos():
    """Возвращает список файлов в папке videos"""
    os.makedirs("videos", exist_ok=True)
    files = sorted(os.listdir("videos"))
    return files

def generate_frames():
    """
    Генерирует MJPEG-поток для FastAPI.
    """
    system = platform.system()
    if system == "Windows":
        command = [
            "ffmpeg",
            "-f", "dshow",
            "-i", "video=AT025",
            "-f", "mjpeg",           # выводим в mjpeg
            "-q:v", "5",             # качество (1=лучше, 31=хуже)
            "-"
        ]

    else:
        command = [
            "ffmpeg",
            "-f", "v4l2",
            "-framerate", "30",
            "-video_size", "1280x720",
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

def fake_stream():
    """Генератор для фейкового MJPEG-потока"""
    img = Image.new("RGB", (640, 480), color=(73, 109, 137))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    frame = buf.getvalue()

    while True:
        yield (b"--frame\r\n"
               b"Content-Type: image/jpeg\r\n\r\n" + frame + b"\r\n")
        time.sleep(0.1)  # имитируем FPS