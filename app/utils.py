import os
import subprocess
from datetime import datetime
import json

PROVISION_FILE = "provision.json"

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
    
def _provision_path():
    # файл хранится рядом с папкой app (текущая рабочая директория — app)
    return os.path.join(os.getcwd(), PROVISION_FILE)

def is_provisioned() -> bool:
    """Возвращает True если устройство provisioned (подключено к Wi-Fi и помечено)."""
    path = _provision_path()
    try:
        if not os.path.exists(path):
            return False
        with open(path, "r") as f:
            data = json.load(f)
        return bool(data.get("provisioned", False))
    except Exception:
        return False

def set_provisioned(value: bool, info: dict | None = None):
    """Записывает статус provisioned и доп.инфо (ssid, ip, timestamp)."""
    path = _provision_path()
    data = {}
    if os.path.exists(path):
        try:
            with open(path, "r") as f:
                data = json.load(f)
        except Exception:
            data = {}
    data["provisioned"] = bool(value)
    if info:
        data.setdefault("info", {}).update(info)
    # добавим timestamp
    from datetime import datetime
    data.setdefault("info", {})["updated_at"] = datetime.now().isoformat()
    with open(path, "w") as f:
        json.dump(data, f)

def get_provision_info() -> dict:
    """Возвращает словарь с инфо (ssid, ip и т.п.) или пустой словарь."""
    path = _provision_path()
    try:
        if not os.path.exists(path):
            return {}
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("info", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}