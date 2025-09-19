import os
import subprocess
from datetime import datetime

def iterfile(path: str):
    with open(path, mode="rb") as file_like:
        while chunk := file_like.read(1024 * 1024):
            yield chunk

def get_video_path(filename: str):
    return os.path.join("videos", filename)

def get_output_filename():
    os.makedirs("videos", exist_ok=True)
    timestamp = datetime.now().strftime("%H-%M-%S_%d.%m.%Y")
    return os.path.join("videos", f"{timestamp}.mp4")

def list_videos():
    os.makedirs("videos", exist_ok=True)
    files = sorted(os.listdir("videos"))
    return files

def get_video_metadata(filepath: str):
    try:
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            filepath
        ]
        result = subprocess.check_output(cmd, text=True).splitlines()
        width, height, fps_raw, duration = result
        fps = eval(fps_raw) if "/" in fps_raw else float(fps_raw)
        return {
            "resolution": f"{width}x{height}",
            "fps": round(fps, 2),
            "duration": round(float(duration), 2)
        }
    except Exception as e:
        return {"error": str(e)}