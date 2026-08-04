import os
import re
import hmac
import secrets
import subprocess
from datetime import datetime
import json
from pathlib import Path

PROVISION_FILENAME = "provision.json"
VIDEOS_DIR = "videos"
API_TOKEN_BYTES = 32

def iterfile(path: str):
    with open(path, mode="rb") as file_like:
        while chunk := file_like.read(1024 * 1024):
            yield chunk

def get_video_path(filename: str):
    video_name = _safe_video_filename(filename)
    return os.path.join(VIDEOS_DIR, video_name)


def _safe_video_filename(filename: str):
    filename = (filename or "").strip()
    if not filename:
        raise ValueError("filename_required")

    basename = os.path.basename(filename)
    if basename != filename:
        raise ValueError("invalid_video_filename")

    if filename in {".", ".."} or ".." in Path(filename).parts:
        raise ValueError("invalid_video_filename")

    if not filename.lower().endswith(".mp4"):
        raise ValueError("invalid_video_filename")

    return filename

def get_output_filename():
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    timestamp = datetime.now().strftime("%H-%M-%S_%d.%m.%Y")
    return os.path.join(VIDEOS_DIR, f"{timestamp}.mp4")

def list_videos():
    os.makedirs(VIDEOS_DIR, exist_ok=True)
    files = sorted(
        filename
        for filename in os.listdir(VIDEOS_DIR)
        if filename.lower().endswith(".mp4")
    )
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


def split_nmcli_escaped(line: str) -> list[str]:
    """Split an nmcli -t --escape yes line without breaking escaped colons."""
    parts = []
    current = []
    escaped = False

    for char in line:
        if escaped:
            current.append(char)
            escaped = False
            continue

        if char == "\\":
            escaped = True
            continue

        if char == ":":
            parts.append("".join(current))
            current = []
            continue

        current.append(char)

    if escaped:
        current.append("\\")
    parts.append("".join(current))
    return parts


def is_wifi_connected() -> bool:
    """Return True only when a Wi-Fi interface is connected."""
    try:
        status = subprocess.check_output(
            ["nmcli", "-t", "-f", "TYPE,STATE", "dev", "status"],
            text=True,
            encoding="utf-8",
            errors="ignore",
        ).strip()
        for line in status.splitlines():
            parts = split_nmcli_escaped(line)
            if len(parts) >= 2 and parts[0] == "wifi":
                return parts[1].lower() == "connected"
        return False
    except Exception:
        return False


def get_wifi_ssid() -> str:
    """Return the active Wi-Fi SSID, or an empty string if disconnected."""
    try:
        ssid_lines = subprocess.check_output(
            ["nmcli", "-t", "--escape", "yes", "-f", "ACTIVE,SSID", "dev", "wifi"],
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        for line in ssid_lines.splitlines():
            parts = split_nmcli_escaped(line)
            if len(parts) >= 2 and parts[0] == "yes":
                return parts[1]
    except Exception:
        pass
    return ""


def get_primary_ipv4() -> str:
    """Return the first global IPv4 address visible on the device."""
    try:
        ip_out = subprocess.check_output(
            ["ip", "-4", "addr", "show", "scope", "global"],
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ip_out)
        return match.group(1) if match else ""
    except Exception:
        return ""


def connect_wifi_nmcli(ssid: str, password: str | None = None, timeout: int = 60) -> dict:
    """
    Connect to Wi-Fi through NetworkManager.

    The password is passed through stdin with nmcli --ask so it does not appear
    in process arguments while provisioning is running.
    """
    ssid = (ssid or "").strip()
    password = password or ""
    if not ssid:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "ssid_required",
            "ip": "",
        }

    try:
        if password:
            proc = subprocess.run(
                ["nmcli", "--ask", "dev", "wifi", "connect", ssid],
                input=f"{password}\n",
                text=True,
                capture_output=True,
                timeout=timeout,
            )
        else:
            proc = subprocess.run(
                ["nmcli", "dev", "wifi", "connect", ssid],
                text=True,
                capture_output=True,
                timeout=timeout,
            )

        ok = proc.returncode == 0
        return {
            "ok": ok,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
            "ip": get_primary_ipv4() if ok else "",
        }
    except Exception as e:
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(e),
            "ip": "",
        }


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
    if info is not None and value:
        data.setdefault("info", {}).update(info)
    elif info is not None:
        data["info"] = dict(info)
    if value:
        data["api_token"] = data.get("api_token") or generate_api_token()
    else:
        data.pop("api_token", None)
    # добавим timestamp
    from datetime import datetime
    data.setdefault("info", {})["updated_at"] = datetime.now().isoformat()

    # Root-owned BLE service and radxa-owned HTTP service both update this file.
    # Write through a temp file + rename so a radxa process can replace an older
    # root-owned file as long as the project directory itself is writable.
    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
        _normalize_provision_file_permissions(path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except Exception:
            pass


def _normalize_provision_file_permissions(path: Path):
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            project_owner = path.parent.stat()
            os.chown(path, project_owner.st_uid, project_owner.st_gid)
        os.chmod(path, 0o664)
    except Exception:
        pass


def generate_api_token() -> str:
    return secrets.token_urlsafe(API_TOKEN_BYTES)


def get_api_token() -> str:
    path = _provision_path()
    try:
        if not path.exists():
            return ""
        with open(path, "r") as f:
            data = json.load(f)
        token = data.get("api_token", "")
        return token if isinstance(token, str) else ""
    except Exception:
        return ""


def verify_api_token(token: str | None) -> bool:
    expected = get_api_token()
    if not expected or not token:
        return False
    return hmac.compare_digest(token, expected)

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
