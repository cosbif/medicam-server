from fastapi import APIRouter, HTTPException, Form
from fastapi.responses import FileResponse, StreamingResponse, Response
from app import camera, utils
import os
import shutil
import platform
import subprocess

router = APIRouter()

# -------------------
# 📼 Запись
# -------------------
@router.post("/start")
async def start_recording():
    return camera.start_recording()

@router.post("/stop")
async def stop_recording():
    return camera.stop_recording()
    
# -------------------
# 🎞 Управление видео
# -------------------
@router.get("/videos")
async def list_videos():
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
async def get_video(filename: str):
    filepath = utils.get_video_path(filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return StreamingResponse(utils.iterfile(filepath), media_type="video/mp4")

@router.get("/download/{filename}")
async def download_video(filename: str):
    filepath = utils.get_video_path(filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=filepath, filename=filename, media_type="video/mp4")

@router.delete("/delete/{filename}")
async def delete_video(filename: str):
    filepath = utils.get_video_path(filename)
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(filepath)
    return {"status": "deleted", "file": filename}

@router.delete("/videos/clear")
async def clear_all_videos():
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
async def get_storage_info():
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
async def get_settings():
    return camera.get_settings()

@router.post("/settings")
async def update_settings(
    resolution: str = Form(None),
    fps: str = Form(None)):
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
async def connect_wifi(ssid: str, password: str):
    return {"status": f"Connected to {ssid}"}

# -------------------
# 🔵 Bluetooth (заглушка)
# -------------------
@router.post("/bluetooth/disconnect")
async def disconnect_bluetooth():
    return {"status": "Bluetooth disconnected"}