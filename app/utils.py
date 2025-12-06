import os
import subprocess
from datetime import datetime
import json
from pathlib import Path

PROVISION_FILENAME = "provision.json"

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
        # получаем JSON-вывод ffprobe для устойчивого парсинга
        cmd = [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-show_entries", "format=duration",
            "-of", "json",
            filepath
        ]
        import json as _json
        result = subprocess.check_output(cmd, text=True)
        data = _json.loads(result)

        stream = data.get("streams", [{}])[0]
        fmt = data.get("format", {})

        width = stream.get("width")
        height = stream.get("height")
        r_frame_rate = stream.get("r_frame_rate", "0/1")
        nums = r_frame_rate.split("/")
        fps = float(nums[0]) / float(nums[1]) if len(nums) == 2 and float(nums[1]) != 0 else 0.0

        duration = float(fmt.get("duration", 0.0))

        return {
            "resolution": f"{width}x{height}" if width and height else "",
            "fps": round(fps, 2),
            "duration": round(duration, 2)
        }
    except Exception as e:
        return {"error": str(e)}
    
def _provision_path():
    # файл хранится в корне проекта (один уровень выше app/)
    project_root = Path(__file__).resolve().parents[1]
    return project_root / PROVISION_FILENAME

def is_provisioned() -> bool:
    """Возвращает True если устройство provisioned (подключено к Wi-Fi и помечено)."""
    path = _provision_path()
    try:
        if not path.exists():
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
    if path.exists():
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
        if not path.exists():
            return {}
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("info", {}) if isinstance(data, dict) else {}
    except Exception:
        return {}