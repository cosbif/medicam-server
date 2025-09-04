from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from app import camera
from app import utils
import os
from fastapi.responses import StreamingResponse
import shutil
import platform
import subprocess

router = APIRouter()

@router.post("/start")
async def start_recording():
    return camera.start_recording()

@router.post("/stop")
async def stop_recording():
    return camera.stop_recording()

@router.get("/videos")
async def list_videos():
    return {"videos": camera.list_videos()}

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

@router.get("/stream")
async def stream_video():
    """Отдаем реальный поток с камеры в 480p для предпросмотра"""
    return StreamingResponse(
        camera.generate_frames(resolution="640x480", fps="15"),
        media_type="multipart/x-mixed-replace; boundary=frame"
    )

@router.get("/storage")
async def get_storage_info():
    """Возвращает информацию о SD-карте"""
    total, used, free = shutil.disk_usage(".")
    return {
        "total_gb": round(total / (1024**3), 2),
        "used_gb": round(used / (1024**3), 2),
        "free_gb": round(free / (1024**3), 2)
    }

@router.get("/settings")
async def get_settings():
    return camera.get_settings()

@router.post("/settings")
async def update_settings(resolution: str = None, fps: str = None):
    return camera.update_settings(resolution, fps)

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
        return {"networks": []}   # 👈 теперь всегда возвращаем список

@router.post("/wifi/connect")
async def connect_wifi(ssid: str, password: str):
    """Подключение к Wi-Fi (пока только заглушка, реально будет работать на Raspberry)"""
    # TODO: здесь можно будет добавить вызов nmcli dev wifi connect
    return {"status": f"Connected to {ssid}"}

@router.post("/bluetooth/disconnect")
async def disconnect_bluetooth():
    """Заглушка отключения Bluetooth"""
    return {"status": "Bluetooth disconnected"}