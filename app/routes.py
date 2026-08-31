'''app/routes.py'''
from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    Form,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field
from app import (
    audio,
    camera,
    diagnostics,
    preview,
    storage_manager,
    update_control,
    updater,
    utils,
    version_info,
)
import asyncio
import io
import math
import os
import platform
import socket
import subprocess
from typing import Literal

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


def require_update_auth(request: Request):
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


@router.get("/ping")
async def ping():
    return {
        "status": "ok",
        "service": "medicam",
        "hostname": socket.gethostname(),
        "protocol": 4,
        "transport": "https",
        "device_id": utils.get_device_id(),
        "device_name": utils.get_device_name(),
        "tls_fingerprint": utils.get_tls_fingerprint(),
        **version_info.get_ping_version(),
    }


@router.get("/version")
async def version(_ok: bool = Depends(require_api_auth)):
    return version_info.get_version_info()

# -------------------
# 📼 Запись
# -------------------
@router.post("/start")
def start_recording(_ok: bool = Depends(require_api_auth)):
    if not diagnostics.begin_recording_start():
        code = (
            "self_test_in_progress"
            if diagnostics.is_self_test_running()
            else "hardware_busy"
        )
        raise HTTPException(status_code=409, detail={"code": code})
    try:
        update = updater.get_update_status()
        if update.get("state") in updater.ACTIVE_STATES:
            raise HTTPException(
                status_code=409,
                detail={"code": "update_in_progress"},
            )
        return camera.start_recording()
    finally:
        diagnostics.end_recording_start()

@router.post("/stop")
def stop_recording(_ok: bool = Depends(require_api_auth)):
    return camera.stop_recording()


@router.get("/recording/status")
def recording_status(_ok: bool = Depends(require_api_auth)):
    return camera.get_recording_status()


# -------------------
# Live SD preview
# -------------------
@router.get("/preview/status")
def preview_status(_ok: bool = Depends(require_api_auth)):
    return preview.get_status()


@router.get("/preview/stream")
async def preview_stream(_ok: bool = Depends(require_api_auth)):
    if not preview.get_status()["enabled"]:
        raise HTTPException(status_code=503, detail="preview_disabled")

    async def frames():
        source = camera.get_preview_source()
        generation = preview.subscribe(**source)
        try:
            while True:
                generation, frame = await asyncio.to_thread(
                    preview.wait_for_frame,
                    generation,
                    2.0,
                )
                if frame is None:
                    # Recover automatically if the camera was connected after
                    # the HTTP stream opened or a helper process restarted.
                    preview.ensure_running(camera.find_camera_device(timeout=0.0))
                    continue
                # A fixed four-byte big-endian length has less overhead and
                # parsing ambiguity than multipart boundaries. Slow clients
                # still receive only the manager's newest completed JPEG.
                # Avoid allocating another FullHD-sized bytes object merely to
                # prepend the framing length on the resource-limited board.
                yield len(frame).to_bytes(4, "big")
                yield frame
        finally:
            preview.unsubscribe()

    return StreamingResponse(
        frames(),
        media_type="application/x-medicam-preview",
        headers={
            "Cache-Control": "no-store",
            "X-Content-Type-Options": "nosniff",
            "X-Medicam-Preview": (
                "display=640x360;fps=10;format=mjpeg;framing=be32;"
                "idle-capture=1280x720;source=dynamic"
            ),
        },
    )


# -------------------
# 🎞 Управление видео
# -------------------
class DeleteVideosRequest(BaseModel):
    filenames: list[str] = Field(min_length=1, max_length=100)


class StoragePolicyRequest(BaseModel):
    mode: Literal["off", "low_space", "keep_last_gb", "keep_last_days"]
    value: float | None = None


class StorageCleanupRequest(BaseModel):
    reclaim_gb: float = Field(gt=0, le=4096)


class TokenRotateRequest(BaseModel):
    new_token: str = Field(min_length=32, max_length=128)


@router.post("/auth/rotate")
async def rotate_auth_token(
    payload: TokenRotateRequest,
    request: Request,
    _ok: bool = Depends(require_api_auth),
):
    try:
        rotated = utils.rotate_api_token(
            _token_from_request(request),
            payload.new_token,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if not rotated:
        raise HTTPException(status_code=409, detail="stale_api_token")
    return {
        "status": "rotated",
        "device_id": utils.get_device_id(),
    }


def _video_sort_value(video: dict, sort: str):
    if sort == "filename":
        return str(video.get("filename", "")).lower()
    if sort == "size":
        return int(video.get("size_bytes", 0) or 0)
    if sort == "duration":
        return float(video.get("duration", 0) or 0)
    if sort == "fps":
        return float(video.get("fps", 0) or 0)
    return str(video.get("created_at", ""))


def _public_video_entry(video: dict) -> dict:
    public_fields = {
        "filename",
        "size_bytes",
        "size_mb",
        "created_at",
        "mtime_ns",
        "metadata_status",
        "thumbnail_ready",
        "resolution",
        "fps",
        "duration",
        "has_audio",
        "audio_codec",
        "audio_channels",
        "audio_sample_rate",
    }
    return {key: value for key, value in video.items() if key in public_fields}


def _ensure_library_mutation_allowed():
    status = camera.get_recording_status()
    if status.get("recording") or status.get("state") == "finalizing":
        raise HTTPException(
            status_code=409,
            detail={"code": "recording_in_progress"},
        )


@router.get("/videos")
async def list_videos(
    background_tasks: BackgroundTasks,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort: Literal["created_at", "filename", "size", "duration", "fps"] = "created_at",
    order: Literal["asc", "desc"] = "desc",
    refresh: bool = False,
    _ok: bool = Depends(require_api_auth),
):
    videos = utils.scan_video_library(force_reload=refresh)
    videos.sort(
        key=lambda item: _video_sort_value(item, sort),
        reverse=order == "desc",
    )
    total = len(videos)
    total_pages = math.ceil(total / page_size) if total else 0
    start = (page - 1) * page_size
    page_videos = videos[start:start + page_size]
    pending_names = [
        item["filename"]
        for item in page_videos
        if item.get("metadata_status") == "loading"
    ]
    claimed = utils.claim_video_metadata_work(pending_names)
    if claimed:
        background_tasks.add_task(utils.populate_video_metadata, claimed)

    return {
        "videos": [_public_video_entry(video) for video in page_videos],
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
            "has_next": page < total_pages,
        },
        "sort": {"field": sort, "order": order},
        "metadata_pending": len(pending_names),
    }


@router.get("/videos/{filename}/thumbnail")
def get_video_thumbnail(filename: str, _ok: bool = Depends(require_api_auth)):
    try:
        filepath = utils.get_video_path(filename)
        thumbnail = utils.get_video_thumbnail_path(filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_video_filename")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    if not thumbnail:
        raise HTTPException(status_code=404, detail="thumbnail_not_available")
    return FileResponse(
        path=thumbnail,
        media_type="image/jpeg",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )


def _parse_byte_range(range_header: str, file_size: int) -> tuple[int, int]:
    value = (range_header or "").strip().lower()
    if not value.startswith("bytes=") or "," in value:
        raise ValueError("invalid_range")
    start_text, separator, end_text = value[6:].partition("-")
    if not separator or (not start_text and not end_text):
        raise ValueError("invalid_range")

    if not start_text:
        suffix_length = int(end_text)
        if suffix_length <= 0:
            raise ValueError("invalid_range")
        start = max(0, file_size - suffix_length)
        end = file_size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else file_size - 1
        end = min(end, file_size - 1)
    if start < 0 or end < start or start >= file_size:
        raise ValueError("invalid_range")
    return start, end


def _iter_file_range(filepath: str, start: int, end: int):
    remaining = end - start + 1
    with open(filepath, "rb") as source:
        source.seek(start)
        while remaining > 0:
            chunk = source.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk

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
        try:
            start, end = _parse_byte_range(range_header, file_size)
        except (TypeError, ValueError):
            raise HTTPException(status_code=416, detail="invalid_range")

        headers = {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Accept-Ranges": "bytes",
            "Content-Length": str(end - start + 1),
            "Cache-Control": "private, no-cache",
        }
        return StreamingResponse(
            _iter_file_range(filepath, start, end),
            status_code=206,
            media_type="video/mp4",
            headers=headers,
        )

    # FileResponse streams in bounded chunks and also supports Range requests
    # in current Starlette versions, avoiding a full multi-gigabyte MP4 read.
    return FileResponse(
        path=filepath,
        media_type="video/mp4",
        headers={
            "Accept-Ranges": "bytes",
            "Cache-Control": "private, no-cache",
        },
    )


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
    _ensure_library_mutation_allowed()
    try:
        filepath = utils.get_video_path(filename)
    except ValueError:
        raise HTTPException(status_code=400, detail="invalid_video_filename")
    if not os.path.exists(filepath):
        raise HTTPException(status_code=404, detail="File not found")
    os.remove(filepath)
    utils.invalidate_video_cache(filename)
    return {"status": "deleted", "file": filename}


@router.post("/videos/delete")
async def delete_videos(
    request: DeleteVideosRequest,
    _ok: bool = Depends(require_api_auth),
):
    _ensure_library_mutation_allowed()
    filenames = list(dict.fromkeys(request.filenames))
    if not filenames or len(filenames) > 100:
        raise HTTPException(status_code=400, detail="invalid_video_selection")

    paths = []
    for filename in filenames:
        try:
            paths.append((filename, utils.get_video_path(filename)))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid_video_filename")

    deleted = []
    missing = []
    for filename, filepath in paths:
        if not os.path.exists(filepath):
            missing.append(filename)
            continue
        os.remove(filepath)
        utils.invalidate_video_cache(filename)
        deleted.append(filename)
    return {"status": "deleted", "files": deleted, "missing": missing}

@router.delete("/videos/clear")
async def clear_all_videos(_ok: bool = Depends(require_api_auth)):
    _ensure_library_mutation_allowed()
    folder = utils.VIDEOS_DIR
    deleted = []
    if os.path.exists(folder):
        for f in utils.list_videos():
            path = utils.get_video_path(f)
            os.remove(path)
            utils.invalidate_video_cache(f)
            deleted.append(f)
    return {"status": "all_deleted", "files": deleted}

# -------------------
# 💾 Хранилище
# -------------------
@router.get("/storage")
async def get_storage_info(_ok: bool = Depends(require_api_auth)):
    return storage_manager.get_storage_info()


@router.post("/storage/policy")
async def update_storage_policy(
    request: StoragePolicyRequest,
    _ok: bool = Depends(require_api_auth),
):
    try:
        policy = storage_manager.update_policy(request.mode, request.value)
    except storage_manager.StoragePolicyError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {"status": "updated", "policy": policy}


@router.post("/storage/cleanup")
async def cleanup_storage(
    request: StorageCleanupRequest,
    _ok: bool = Depends(require_api_auth),
):
    _ensure_library_mutation_allowed()
    return storage_manager.reclaim_space(
        round(request.reclaim_gb * storage_manager.GIB)
    )

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
# 🩺 Диагностика и support
# -------------------
@router.get("/diagnostics/health")
async def diagnostic_health(_ok: bool = Depends(require_api_auth)):
    return await asyncio.to_thread(diagnostics.get_health)


@router.post("/diagnostics/self-test")
async def diagnostic_self_test(_ok: bool = Depends(require_api_auth)):
    try:
        return await asyncio.to_thread(diagnostics.run_self_test)
    except diagnostics.SelfTestBusyError as error:
        raise HTTPException(status_code=409, detail={"code": str(error)}) from error


@router.get("/diagnostics/bundle")
async def diagnostic_bundle(_ok: bool = Depends(require_api_auth)):
    content, filename = await asyncio.to_thread(diagnostics.build_diagnostic_bundle)
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/zip",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Content-Length": str(len(content)),
            "Cache-Control": "no-store",
        },
    )


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
    wifi_connected = await asyncio.to_thread(utils.is_wifi_connected)
    payload = {
        "provisioned": utils.is_provisioned(),
        "device": {
            "id": utils.get_device_id(),
            "name": utils.get_device_name(),
        },
        "protocol": 4,
        "transport": "https",
        "tls_fingerprint": utils.get_tls_fingerprint(),
        "wifi": {
            "connected": wifi_connected,
        },
    }
    if authenticated:
        payload["info"] = utils.get_provision_info()
        payload["wifi"]["ssid"], payload["wifi"]["ip"] = await asyncio.gather(
            asyncio.to_thread(utils.get_wifi_ssid),
            asyncio.to_thread(utils.get_primary_ipv4),
        )
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
    return {
        "status": "recovery_started",
        "expires_at": expires_at,
        # BLE is always available; keep this field for older app versions that
        # opened a bounded recovery window before scanning.
        "ble_start": {"ok": True, "status": "already_active"},
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
    utils.request_ble_refresh()
    return {
        "status": "reset",
        "ble_restart": {"ok": True, "status": "requested"},
        "ble_service": _systemctl_status(BLE_SERVICE),
    }


# -------------------
# ⏻ POWER CONTROL
# -------------------

@router.post("/system/poweroff")
async def system_poweroff(_ok: bool = Depends(require_api_auth)):
    recording = camera.get_recording_status()
    if recording.get("capture_active") or recording.get("state") in {
        "starting",
        "recording",
        "interrupted",
        "finalizing",
    }:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "recording_in_progress",
                "state": recording.get("state"),
                "message": "Stop or recover the current recording before power off",
            },
        )

    update = updater.get_update_status()
    if update.get("state") in updater.ACTIVE_STATES:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "update_in_progress",
                "state": update.get("state"),
                "message": "Wait for the signed update to finish before power off",
            },
        )

    utils.request_poweroff()
    return {"status": "poweroff_requested"}

# -------------------
# 🔄 SIGNED OTA UPDATE
# -------------------

@router.get("/update/check")
async def update_check(_ok: bool = Depends(require_update_auth)):
    return updater.check_for_update()


@router.get("/update/status")
async def update_status(_ok: bool = Depends(require_update_auth)):
    return updater.get_update_status()


@router.post("/update/apply")
async def update_apply(_ok: bool = Depends(require_update_auth)):
    try:
        return update_control.start_signed_update()
    except update_control.UpdateStartBlockedError as error:
        messages = {
            "recording_in_progress": (
                "Stop or recover the current recording before updating"
            ),
            "update_in_progress": "An update is already running",
            "device_busy": "Another camera hardware operation is running",
        }
        raise HTTPException(
            status_code=409,
            detail={
                "code": error.code,
                "state": error.state,
                "message": messages.get(error.code, "Update cannot be started"),
            },
        ) from error
