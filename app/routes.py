from fastapi import APIRouter, HTTPException, Form, Depends, Request
from fastapi.responses import FileResponse, Response
from app import camera, utils
import os
import shutil
import platform
import subprocess

def require_provisioned():
    '''if not utils.is_provisioned():
        raise HTTPException(status_code=403, detail="device_not_provisioned")'''
    return True

router = APIRouter()

# -------------------
# 📼 Запись
# -------------------
@router.post("/start")
async def start_recording(_ok: bool = Depends(require_provisioned)):
    return camera.start_recording()

@router.post("/stop")
async def stop_recording(_ok: bool = Depends(require_provisioned)):
    return camera.stop_recording()
    
# -------------------
# 🎞 Управление видео
# -------------------
@router.get("/videos")
async def list_videos(_ok: bool = Depends(require_provisioned)):
    videos = utils.list_videos()
    video_info = []
    for f in videos:
        path = utils.get_video_path(f)
        meta = utils.get_video_metadata(path)
        video_info.append({
            "filename": f,
            "size_mb": round(os.path.getsize(path) / (1024*1024), 2),
            **meta
        })
    return {"videos": video_info}

@router.get("/videos/{filename}")
async def get_video(filename: str, request: Request, _ok: bool = Depends(require_provisioned)):
    filepath = utils.get_video_path(filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    file_size = os.path.getsize(filepath)
    range_header = request.headers.get("range")

    if range_header:
        # Пример: Range: bytes=0-1023
        range_value = range_header.strip().lower().replace("bytes=", "")
        start, end = range_value.split("-") if "-" in range_value else (0, "")
        start = int(start) if start else 0
        end = int(end) if end else file_size - 1
        end = min(end, file_size - 1)

        with open(filepath, "rb") as f:
            f.seek(start)
            data = f.read(end - start + 1)

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Content-Type": "video/mp4",
        }
        return Response(content=data, status_code=206, headers=headers)

    # Без Range-запроса (например Android)
    with open(filepath, "rb") as f:
        data = f.read()

    headers = {
        "Accept-Ranges": "bytes",
        "Content-Length": str(file_size),
        "Content-Type": "video/mp4",
    }
    return Response(content=data, headers=headers)


@router.get("/download/{filename}")
async def download_video(filename: str, _ok: bool = Depends(require_provisioned)):
    filepath = utils.get_video_path(filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=filepath, filename=filename, media_type="video/mp4")

@router.delete("/delete/{filename}")
async def delete_video(filename: str, _ok: bool = Depends(require_provisioned)):
    filepath = utils.get_video_path(filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(filepath)
    return {"status": "deleted", "file": filename}

@router.delete("/videos/clear")
async def clear_all_videos(_ok: bool = Depends(require_provisioned)):
    folder = "videos"
    deleted = []
    if os.path.exists(folder):
        for f in os.listdir(folder):
            path = os.path.join(folder, f)
            os.remove(path)
            deleted.append(f)
    return {"status": "all_deleted", "files": deleted}

# -------------------
# 💾 Хранилище
# -------------------
@router.get("/storage")
async def get_storage_info(_ok: bool = Depends(require_provisioned)):
    total, used, free = shutil.disk_usage(".")
    free_gb = round(free / (1024 ** 3), 2)
    return {
        "total": round(total / (1024 ** 3), 2),
        "used": round(used / (1024 ** 3), 2),
        "free": free_gb,
        "low_space": free_gb < 1,
    }

# -------------------
# ⚙️ Настройки камеры
# -------------------
@router.get("/settings")
async def get_settings(_ok: bool = Depends(require_provisioned)):
    return camera.get_settings()

@router.post("/settings")
async def update_settings(
    resolution: str = Form(None),
    fps: str = Form(None),
    _ok: bool = Depends(require_provisioned)):
    return camera.update_settings(resolution, fps)

# -------------------
# 📡 Wi-Fi
# -------------------
@router.get("/wifi")
async def list_wifi():
    system = platform.system()
    try:
        if system == "Windows":
            result = subprocess.check_output(
                ["netsh", "wlan", "show", "networks", "mode=bssid"],
                shell=True, text=True, encoding="utf-8", errors="ignore"
            )
            networks = []
            for line in result.splitlines():
                if "SSID" in line and ":" in line:
                    ssid = line.split(":", 1)[1].strip()
                    if ssid and ssid not in networks:
                        networks.append(ssid)
            return {"networks": networks}

        elif system == "Linux":
            result = subprocess.check_output(
                ["nmcli", "-t", "-f", "SSID", "dev", "wifi"],
                text=True, encoding="utf-8", errors="ignore"
            )
            networks = [ssid.strip() for ssid in result.splitlines() if ssid.strip()]
            return {"networks": networks}

        else:
            return {"networks": []}

    except Exception as e:
        print(f"Ошибка при сканировании Wi-Fi: {e}")
        return {"networks": []}

@router.post("/wifi/connect")
async def connect_wifi(ssid: str = Form(...), password: str = Form(None)):
    # Попытка подключиться через nmcli
    try:
        if password:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid, "password", password]
        else:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
        success = proc.returncode == 0
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()
        if success:
            # определим ip (если есть) — сначала ищем первый глобальный IPv4
            try:
                ip_out = subprocess.check_output(["ip", "-4", "addr", "show", "scope", "global"], text=True)
                import re
                m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ip_out)
                ip = m.group(1) if m else ""
            except Exception:
                ip = ""
            utils.set_provisioned(True, {"ssid": ssid, "ip": ip})
            return {"status": "connected", "stdout": stdout}
        else:
            return {"status": "error", "stdout": stdout, "stderr": stderr}
    except Exception as e:
        return {"status": "error", "details": str(e)}
    
@router.get("/wifi/status")
async def wifi_status():
    try:
        state = subprocess.check_output(["nmcli", "-t", "-f", "STATE", "g"], text=True).strip()
        connected = "connected" in state.lower()
        ssid = ""
        if connected:
            ssid_lines = subprocess.check_output(["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"], text=True)
            for line in ssid_lines.splitlines():
                if line.startswith("yes:"):
                    ssid = line.split(":", 1)[1]
                    break
        return {"connected": connected, "ssid": ssid}
    except Exception as e:
        return {"connected": False, "error": str(e)}

# -------------------
# 🔵 Bluetooth (заглушка)
# -------------------
@router.get("/provision/status")
async def provision_status():
    return {
        "provisioned": utils.is_provisioned(),
        "info": utils.get_provision_info()
    }

@router.post("/provision/reset")
async def provision_reset():
    # сбросим статус (например для теста)
    utils.set_provisioned(False, {})
    return {"status": "reset"}
