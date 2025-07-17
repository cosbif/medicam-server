import subprocess
from datetime import datetime
import os
from .config import VIDEO_DIR

recording_process = None

def start_recording():
    global recording_process
    if recording_process is not None:
        return None

    filename = datetime.now().strftime("%Y%m%d_%H%M%S") + ".mp4"
    filepath = os.path.join(VIDEO_DIR, filename)

    recording_process = subprocess.Popen([
        "ffmpeg", "-f", "v4l2", "-video_size", "3840x2160",
        "-i", "/dev/video0", "-c:v", "libx264", "-preset", "veryfast",
        "-crf", "23", filepath
    ])

    return filename

def stop_recording():
    global recording_process
    if recording_process:
        recording_process.terminate()
        recording_process = None
        return True
    return False
