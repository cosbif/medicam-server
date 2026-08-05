'''app/routes.py'''
from fastapi import APIRouter, HTTPException, Form, Depends, Request
from fastapi.responses import FileResponse, Response
from app import audio, camera, utils, updater
import ipaddress
import os
import shutil
import platform
import socket
import subprocess
from app.updater import check_for_update, apply_update

BLE_SERVICE = "medicam-ble.service"
AUTH_HEADER = "x-medicam-token"


def _token_from_request(request: Request):
    return request.headers.get(AUTH_HEADER)


def _is_authenticated(request: Request):
    return utils.verify_api_token(_token_from_request(request))


def require_api_auth(request: Request):
    if not utils.is_provisioned():
        raise HTTPException(status_code=403, detail="device_not_provisioned")
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="invalid_api_token")
    return True


def _is_loopback_request(request: Request):
    client_host = request.client.host if request.client else ""
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return client_host in {"localhost"}


def require_update_auth(request: Request):
    if _is_loopback_request(request):
        return True
    return require_api_auth(request)

router = APIRouter()


def _systemctl_status(unit: str):
    try:
        proc = subprocess.run(
            ["/bin/systemctl", "is-active", unit],
            text=True,
            capture_output=True,
            timeout=5,
        )
        status = proc.stdout.strip() or proc.stderr.strip() or "unknown"
        return {
            "ok": proc.returncode == 0,
            "status": status,
        }
    except Exception as e:
        return {
            "ok": False,
            "status": "unavailable",
            "error": str(e),
        }


def _systemctl_action(action: str, unit: str):
    try:
        proc = subprocess.run(
            ["sudo", "/bin/systemctl", action, unit],
            text=True,
            capture_output=True,
            timeout=10,
        )
        return {
            "ok": proc.returncode == 0,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
    except Exception as e:
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(e),
        }


@router.get("/ping")
async def ping():
    return {
        "status": "ok",
        "service": "medicam",
        "hostname": socket.gethostname(),
    }

# -------------------
# 📼 Запись
# -------------------
@router.post("/start")
def start_recording(_ok: bool = Depends(require_api_auth)):
    return camera.start_recording()

@router.post("/stop")
def stop_recording(_ok: bool = Depends(require_api_auth)):
    return camera.stop_recording()


@router.get("/recording/status")
def recording_status(_ok: bool = Depends(require_api_auth)):
    return camera.get_recording_status()


# -------------------
# 🎞 Управление видео
# -------------------
@router.get("/videos")
async def list_videos(_ok: bool = Depends(require_api_auth)):
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
async def get_video(filename: str, request: Request, _ok: bool = Depends(require_api_auth)):
    try:
        filepath = utils.get_video_path(filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_video_filename")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")

    file_size = os.path.getsize(filepath)
    range_header = request.headers.get("range")

    if range_header:
        # Пример: Range: bytes=0-1023
        try:
            range_value = range_header.strip().lower().replace("bytes=", "")
            start, end = range_value.split("-") if "-" in range_value else (0, "")
            start = int(start) if start else 0
            end = int(end) if end else file_size - 1
            end = min(end, file_size - 1)
            if start < 0 or end < start or start >= file_size:
                raise ValueError
        except ValueError:
            raise HTTPException(status_code=416, detail="invalid_range")

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
async def download_video(filename: str, _ok: bool = Depends(require_api_auth)):
    try:
        filepath = utils.get_video_path(filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_video_filename")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(path=filepath, filename=filename, media_type="video/mp4")

@router.delete("/delete/{filename}")
async def delete_video(filename: str, _ok: bool = Depends(require_api_auth)):
    try:
        filepath = utils.get_video_path(filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_video_filename")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(filepath)
    return {"status": "deleted", "file": filename}

@router.delete("/videos/clear")
async def clear_all_videos(_ok: bool = Depends(require_api_auth)):
    folder = utils.VIDEOS_DIR
    deleted = []
    if os.path.exists(folder):
        for f in utils.list_videos():
            path = utils.get_video_path(f)
            os.remove(path)
            deleted.append(f)
    return {"status": "all_deleted", "files": deleted}

# -------------------
# 💾 Хранилище
# -------------------
@router.get("/storage")
async def get_storage_info(_ok: bool = Depends(require_api_auth)):
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
async def get_settings(_ok: bool = Depends(require_api_auth)):
    return camera.get_settings()

@router.post("/settings")
async def update_settings(
    resolution: str = Form(None),
    fps: str = Form(None),
    audio_enabled: bool = Form(None),
    audio_device: str = Form(None),
    _ok: bool = Depends(require_api_auth)):
    return camera.update_settings(
        resolution,
        fps,
        audio_enabled,
        audio_device,
    )


# -------------------
# 🎙️ Звук
# -------------------
@router.get("/audio/devices")
async def audio_devices(_ok: bool = Depends(require_api_auth)):
    settings = camera.get_settings()
    devices = audio.list_capture_devices()
    selected = audio.resolve_capture_device(settings.get("audio_device", "auto"))
    return {
        "enabled": bool(settings.get("audio_enabled", True)),
        "configured_device": settings.get("audio_device", "auto"),
        "selected_device": selected,
        "devices": devices,
        "format": {
            "codec": "aac",
            "sample_rate": audio.AUDIO_SAMPLE_RATE,
            "channels": audio.AUDIO_CHANNELS,
            "bitrate": audio.AUDIO_BITRATE,
        },
    }


@router.post("/audio/test")
async def audio_test(
    device: str = Form(None),
    duration_seconds: int = Form(2),
    _ok: bool = Depends(require_api_auth),
):
    if camera.get_recording_status()["recording"]:
        raise HTTPException(status_code=409, detail="recording_in_progress")
    configured = device or camera.get_settings().get("audio_device", "auto")
    try:
        return audio.measure_audio_level(configured, duration_seconds)
    except audio.AudioError as error:
        status_code = 409 if error.code == "audio_device_busy" else 400
        raise HTTPException(
            status_code=status_code,
            detail=error.code,
        ) from error


# -------------------
# 📡 Wi-Fi
# -------------------
@router.get("/wifi")
async def list_wifi(_ok: bool = Depends(require_api_auth)):
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
async def connect_wifi(ssid: str = Form(...), password: str = Form(None), _ok: bool = Depends(require_api_auth)):
    result = utils.connect_wifi_nmcli(ssid, password, timeout=60)
    if result["ok"]:
        utils.set_provisioned(True, {"ssid": ssid, "ip": result["ip"]})
        return {
            "status": "connected",
            "ip": result["ip"],
        }

    return {
        "status": "error",
        "error_code": result.get("error_code", "connection_failed"),
    }
    
@router.get("/wifi/status")
async def wifi_status(_ok: bool = Depends(require_api_auth)):
    connected = utils.is_wifi_connected()
    return {
        "connected": connected,
        "ssid": utils.get_wifi_ssid() if connected else "",
        "ip": utils.get_primary_ipv4() if connected else "",
    }

# -------------------
# 🔵 Bluetooth (заглушка)
# -------------------
@router.get("/provision/status")
async def provision_status(request: Request):
    authenticated = _is_authenticated(request)
    payload = {
        "provisioned": utils.is_provisioned(),
        "device": {
            "id": utils.get_device_id(),
            "name": utils.get_device_name(),
        },
        "protocol": 3,
        "wifi": {
            "connected": utils.is_wifi_connected(),
        },
    }
    if authenticated:
        payload["info"] = utils.get_provision_info()
        payload["wifi"]["ssid"] = utils.get_wifi_ssid()
        payload["wifi"]["ip"] = utils.get_primary_ipv4()
        payload["ble_service"] = _systemctl_status(BLE_SERVICE)
        payload["recovery"] = {
            "active": utils.is_ble_recovery_active(),
            "expires_at": utils.get_ble_recovery_until(),
        }
    return payload


@router.post("/provision/recovery/start")
async def provision_recovery_start(
    duration_seconds: int = Form(600),
    _ok: bool = Depends(require_api_auth),
):
    expires_at = utils.start_ble_recovery(duration_seconds)
    start = _systemctl_action("start", BLE_SERVICE)
    return {
        "status": "recovery_started",
        "expires_at": expires_at,
        "ble_start": start,
        "ble_service": _systemctl_status(BLE_SERVICE),
    }


@router.post("/provision/recovery/stop")
async def provision_recovery_stop(_ok: bool = Depends(require_api_auth)):
    utils.stop_ble_recovery()
    return {
        "status": "recovery_stopped",
        "ble_service": _systemctl_status(BLE_SERVICE),
    }

@router.post("/provision/reset")
async def provision_reset(request: Request):
    if utils.is_provisioned() and not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="invalid_api_token")
    # Rotate ownership credentials and expose BLE for the next owner.
    utils.set_provisioned(False, {})
    restart = _systemctl_action("restart", BLE_SERVICE)
    return {
        "status": "reset",
        "ble_restart": restart,
        "ble_service": _systemctl_status(BLE_SERVICE),
    }

# -------------------
# 🔄 OTA UPDATE (git pull)
# -------------------

@router.get("/update/check")
async def update_check(_ok: bool = Depends(require_update_auth)):
    """
    Возвращает текущий и удалённый git commit.
    """
    return updater.check_for_update()


@router.post("/update/apply")
async def update_apply(_ok: bool = Depends(require_update_auth)):
    """
    Выполняет git pull (fetch + reset) и перезапуск сервиса.
    """
    recording = camera.get_recording_status()
    if recording.get("capture_active") or recording.get("state") in {
        "starting",
        "recording",
        "finalizing",
    }:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "recording_in_progress",
                "state": recording["state"],
                "message": "Stop or recover the current recording before updating",
            },
        )
    result = updater.apply_update()
    if not result["ok"]:
        raise HTTPException(status_code=500, detail=result)

    return result
