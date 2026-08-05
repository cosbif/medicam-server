import os
import re
import hmac
import secrets
import subprocess
import hashlib
import socket
import threading
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

PROVISION_FILENAME = "provision.json"
VIDEOS_DIR = "videos"
API_TOKEN_BYTES = 32
VIDEO_METADATA_CACHE_LIMIT = 512
VIDEO_INDEX_FILENAME = ".media-index.json"
VIDEO_INDEX_VERSION = 1
VIDEO_THUMBNAILS_DIRNAME = ".thumbnails"
DEFAULT_BLE_RECOVERY_SECONDS = 10 * 60
MAX_BLE_RECOVERY_SECONDS = 30 * 60
BOOT_PAIRING_WINDOW_SECONDS = 5 * 60
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))

_VIDEO_METADATA_CACHE = {}
_VIDEO_INDEX_CACHE = None
_VIDEO_INDEX_CACHE_PATH = None
_VIDEO_INDEX_LOCK = threading.RLock()
_VIDEO_METADATA_IN_PROGRESS = set()

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

    if "\x00" in filename or len(filename.encode("utf-8")) > 255:
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


def _video_index_path() -> Path:
    return Path(VIDEOS_DIR) / VIDEO_INDEX_FILENAME


def _video_thumbnails_dir() -> Path:
    return Path(VIDEOS_DIR) / VIDEO_THUMBNAILS_DIRNAME


def _cached_thumbnail_path(thumbnail: object) -> Path | None:
    if not isinstance(thumbnail, str):
        return None
    if Path(thumbnail).name != thumbnail or not thumbnail.lower().endswith(".jpg"):
        return None
    return _video_thumbnails_dir() / thumbnail


def _empty_video_index() -> dict:
    return {"version": VIDEO_INDEX_VERSION, "entries": {}}


def _load_video_index_locked(force_reload: bool = False) -> dict:
    global _VIDEO_INDEX_CACHE, _VIDEO_INDEX_CACHE_PATH

    path = _video_index_path().resolve()
    if _VIDEO_INDEX_CACHE_PATH != path:
        _VIDEO_INDEX_CACHE = None
        _VIDEO_INDEX_CACHE_PATH = path
    if _VIDEO_INDEX_CACHE is not None and not force_reload:
        return _VIDEO_INDEX_CACHE

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or data.get("version") != VIDEO_INDEX_VERSION
            or not isinstance(data.get("entries"), dict)
        ):
            raise ValueError("unsupported_video_index")
        _VIDEO_INDEX_CACHE = data
    except (OSError, ValueError, json.JSONDecodeError):
        _VIDEO_INDEX_CACHE = _empty_video_index()
    return _VIDEO_INDEX_CACHE


def _save_video_index_locked(index: dict) -> None:
    path = _video_index_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.tmp")
    payload = json.dumps(index, ensure_ascii=False, sort_keys=True, indent=2)
    with open(temporary, "w", encoding="utf-8") as output:
        output.write(payload)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _video_fingerprint(path: str) -> tuple[int, int]:
    stat_result = os.stat(path)
    return stat_result.st_mtime_ns, stat_result.st_size


def _basic_video_entry(filename: str, path: str) -> dict:
    mtime_ns, size_bytes = _video_fingerprint(path)
    created_at = datetime.fromtimestamp(
        mtime_ns / 1_000_000_000,
        tz=timezone.utc,
    ).isoformat()
    return {
        "filename": filename,
        "size_bytes": size_bytes,
        "size_mb": round(size_bytes / (1024 * 1024), 2),
        "created_at": created_at,
        "mtime_ns": mtime_ns,
        "metadata_status": "loading",
        "thumbnail_ready": False,
        "thumbnail_status": "loading",
    }


def scan_video_library(force_reload: bool = False) -> list[dict]:
    """Return a fast filesystem-backed snapshot and persist cache changes.

    This function only stats MP4 files. Expensive ffprobe and thumbnail work is
    deliberately performed by ``populate_video_metadata`` after the HTTP list
    response has already been sent.
    """
    with _VIDEO_INDEX_LOCK:
        index = _load_video_index_locked(force_reload=force_reload)
        entries = index["entries"]
        filenames = list_videos()
        filename_set = set(filenames)
        dirty = False

        for stale_name in set(entries) - filename_set:
            thumbnail = entries.get(stale_name, {}).get("thumbnail_file")
            thumbnail_path = _cached_thumbnail_path(thumbnail)
            if thumbnail_path:
                try:
                    thumbnail_path.unlink()
                except FileNotFoundError:
                    pass
            entries.pop(stale_name, None)
            dirty = True

        for filename in filenames:
            path = get_video_path(filename)
            mtime_ns, size_bytes = _video_fingerprint(path)
            cached = entries.get(filename)
            fingerprint_matches = (
                isinstance(cached, dict)
                and cached.get("mtime_ns") == mtime_ns
                and cached.get("size_bytes") == size_bytes
            )
            if not fingerprint_matches:
                entries[filename] = _basic_video_entry(filename, path)
                dirty = True
            elif cached.get("metadata_status") == "error" and force_reload:
                # An explicit pull-to-refresh retries transient ffprobe errors
                # without creating an unbounded background retry loop.
                cached["metadata_status"] = "loading"
                cached.pop("metadata_error", None)
                dirty = True
            elif cached.get("thumbnail_ready"):
                thumbnail = cached.get("thumbnail_file")
                thumbnail_path = _cached_thumbnail_path(thumbnail)
                if not thumbnail_path or not thumbnail_path.is_file():
                    cached["thumbnail_ready"] = False
                    cached["thumbnail_status"] = "loading"
                    dirty = True
            elif cached.get("thumbnail_status") == "error" and force_reload:
                cached["thumbnail_status"] = "loading"
                cached.pop("thumbnail_error", None)
                dirty = True
            elif not cached.get("thumbnail_status"):
                # Upgrade indexes written before thumbnail status existed.
                cached["thumbnail_status"] = "loading"
                dirty = True

        if dirty:
            _save_video_index_locked(index)
        return [dict(entries[filename]) for filename in filenames]


def claim_video_metadata_work(filenames: list[str]) -> list[str]:
    """Claim missing metadata jobs so repeated list polling does not duplicate work."""
    claimed = []
    with _VIDEO_INDEX_LOCK:
        index = _load_video_index_locked()
        for filename in filenames:
            entry = index["entries"].get(filename)
            if (
                isinstance(entry, dict)
                and (
                    entry.get("metadata_status") == "loading"
                    or entry.get("thumbnail_status") == "loading"
                )
                and filename not in _VIDEO_METADATA_IN_PROGRESS
            ):
                _VIDEO_METADATA_IN_PROGRESS.add(filename)
                claimed.append(filename)
    return claimed


def _thumbnail_filename(filename: str) -> str:
    digest = hashlib.sha256(filename.encode("utf-8")).hexdigest()[:24]
    return f"{digest}.jpg"


def _generate_video_thumbnail(filepath: str, filename: str, duration: float) -> str:
    thumbnail_dir = _video_thumbnails_dir()
    thumbnail_dir.mkdir(parents=True, exist_ok=True)
    thumbnail_name = _thumbnail_filename(filename)
    output = thumbnail_dir / thumbnail_name
    temporary = output.with_suffix(".tmp.jpg")
    seek_seconds = min(5.0, max(0.0, duration * 0.1))
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{seek_seconds:.3f}",
                "-i",
                filepath,
                "-frames:v",
                "1",
                "-vf",
                "scale=480:-2",
                "-q:v",
                "4",
                "-y",
                str(temporary),
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=30,
        )
        os.replace(temporary, output)
        return thumbnail_name
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def populate_video_metadata(filenames: list[str]) -> None:
    """Populate ffprobe data and thumbnails for previously claimed videos."""
    for filename in filenames:
        try:
            filepath = get_video_path(filename)
            expected_mtime_ns, expected_size = _video_fingerprint(filepath)
            metadata = get_video_metadata(filepath)
            if "error" in metadata:
                raise RuntimeError(metadata["error"])

            thumbnail_name = ""
            thumbnail_error = ""
            try:
                thumbnail_name = _generate_video_thumbnail(
                    filepath,
                    filename,
                    float(metadata.get("duration", 0.0)),
                )
            except Exception as error:
                # Metadata is still useful when a corrupt first frame prevents
                # preview generation; the app shows a deterministic placeholder.
                thumbnail_error = str(error)

            with _VIDEO_INDEX_LOCK:
                index = _load_video_index_locked()
                entry = index["entries"].get(filename)
                if (
                    isinstance(entry, dict)
                    and entry.get("mtime_ns") == expected_mtime_ns
                    and entry.get("size_bytes") == expected_size
                ):
                    entry.update(metadata)
                    entry["metadata_status"] = "ready"
                    entry["thumbnail_ready"] = bool(thumbnail_name)
                    if thumbnail_name:
                        entry["thumbnail_file"] = thumbnail_name
                        entry["thumbnail_status"] = "ready"
                    else:
                        entry.pop("thumbnail_file", None)
                        entry["thumbnail_status"] = "error"
                    if thumbnail_error:
                        entry["thumbnail_error"] = thumbnail_error
                    else:
                        entry.pop("thumbnail_error", None)
                    _save_video_index_locked(index)
        except Exception as error:
            with _VIDEO_INDEX_LOCK:
                index = _load_video_index_locked()
                entry = index["entries"].get(filename)
                if isinstance(entry, dict):
                    entry["metadata_status"] = "error"
                    entry["metadata_error"] = str(error)
                    entry["thumbnail_status"] = "error"
                    _save_video_index_locked(index)
        finally:
            with _VIDEO_INDEX_LOCK:
                _VIDEO_METADATA_IN_PROGRESS.discard(filename)


def get_video_thumbnail_path(filename: str) -> str | None:
    """Return an existing cached thumbnail, generating it on demand if needed."""
    filename = _safe_video_filename(filename)
    entries = {entry["filename"]: entry for entry in scan_video_library()}
    entry = entries.get(filename)
    if entry is None:
        return None

    thumbnail = entry.get("thumbnail_file")
    if entry.get("thumbnail_ready") and thumbnail:
        path = _cached_thumbnail_path(thumbnail)
        if path and path.is_file():
            return str(path)

    claimed = claim_video_metadata_work([filename])
    if claimed:
        populate_video_metadata(claimed)
    with _VIDEO_INDEX_LOCK:
        refreshed = _load_video_index_locked()["entries"].get(filename, {})
        thumbnail = refreshed.get("thumbnail_file")
        if refreshed.get("thumbnail_ready") and thumbnail:
            path = _cached_thumbnail_path(thumbnail)
            if path and path.is_file():
                return str(path)
    return None


def invalidate_video_cache(filename: str) -> None:
    filename = _safe_video_filename(filename)
    filepath = get_video_path(filename)
    with _VIDEO_INDEX_LOCK:
        index = _load_video_index_locked()
        entry = index["entries"].pop(filename, None)
        if entry:
            thumbnail = entry.get("thumbnail_file")
            thumbnail_path = _cached_thumbnail_path(thumbnail)
            if thumbnail_path:
                try:
                    thumbnail_path.unlink()
                except FileNotFoundError:
                    pass
            _save_video_index_locked(index)
        _VIDEO_METADATA_IN_PROGRESS.discard(filename)
        stale_keys = [key for key in _VIDEO_METADATA_CACHE if key[0] == filepath]
        for key in stale_keys:
            _VIDEO_METADATA_CACHE.pop(key, None)

def get_video_metadata(filepath: str):
    try:
        stat_result = os.stat(filepath)
        cache_key = (
            filepath,
            stat_result.st_mtime_ns,
            stat_result.st_size,
        )
    except OSError as e:
        return {"error": str(e)}

    cached = _VIDEO_METADATA_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    try:
        # получаем JSON-вывод ffprobe для устойчивого парсинга
        cmd = [
            "ffprobe", "-v", "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,r_frame_rate,channels,sample_rate",
            "-show_entries", "format=duration",
            "-of", "json",
            filepath
        ]
        import json as _json
        result = subprocess.check_output(cmd, text=True, timeout=5)
        data = _json.loads(result)

        streams = data.get("streams", [])
        video_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "video"),
            {},
        )
        audio_stream = next(
            (stream for stream in streams if stream.get("codec_type") == "audio"),
            None,
        )
        fmt = data.get("format", {})

        width = video_stream.get("width")
        height = video_stream.get("height")
        r_frame_rate = video_stream.get("r_frame_rate", "0/1")
        nums = r_frame_rate.split("/")
        fps = float(nums[0]) / float(nums[1]) if len(nums) == 2 and float(nums[1]) != 0 else 0.0

        duration = float(fmt.get("duration", 0.0))

        metadata = {
            "resolution": f"{width}x{height}" if width and height else "",
            "fps": round(fps, 2),
            "duration": round(duration, 2),
            "has_audio": audio_stream is not None,
            "audio_codec": audio_stream.get("codec_name", "") if audio_stream else "",
            "audio_channels": audio_stream.get("channels", 0) if audio_stream else 0,
            "audio_sample_rate": int(audio_stream.get("sample_rate", 0))
            if audio_stream and str(audio_stream.get("sample_rate", "")).isdigit()
            else 0,
        }
        if len(_VIDEO_METADATA_CACHE) >= VIDEO_METADATA_CACHE_LIMIT:
            _VIDEO_METADATA_CACHE.clear()
        _VIDEO_METADATA_CACHE[cache_key] = dict(metadata)
        return metadata
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
            "error_code": "ssid_required",
            "ip": "",
        }
    if len(ssid.encode("utf-8")) > 32:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "ssid_too_long",
            "error_code": "ssid_too_long",
            "ip": "",
        }
    if password and not _is_valid_wifi_password(password):
        return {
            "ok": False,
            "stdout": "",
            "stderr": "invalid_wifi_password",
            "error_code": "invalid_password",
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
            "error_code": "" if ok else classify_nmcli_error(proc.stderr),
            "ip": get_primary_ipv4() if ok else "",
        }
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "stdout": "",
            "stderr": "connection_timeout",
            "error_code": "connection_timeout",
            "ip": "",
        }
    except Exception as e:
        return {
            "ok": False,
            "stdout": "",
            "stderr": str(e),
            "error_code": "connection_failed",
            "ip": "",
        }


def _is_valid_wifi_password(password: str) -> bool:
    """Accept open networks, WPA passphrases, and 64-character PSKs."""
    if not password:
        return True
    if 8 <= len(password) <= 63:
        return True
    return len(password) == 64 and bool(re.fullmatch(r"[0-9a-fA-F]{64}", password))


def classify_nmcli_error(stderr: str) -> str:
    text = (stderr or "").lower()
    if any(
        marker in text
        for marker in (
            "invalid secrets",
            "no secrets",
            "wrong password",
            "passwords or encryption keys are required",
            "802-11-wireless-security",
        )
    ):
        return "invalid_password"
    if "no network with ssid" in text or "network could not be found" in text:
        return "network_not_found"
    if "timeout" in text or "timed out" in text:
        return "connection_timeout"
    if "not authorized" in text or "permission denied" in text:
        return "not_authorized"
    return "connection_failed"


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
    data.pop("ble_recovery_until", None)
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


def get_device_id() -> str:
    """Return a stable, non-secret identifier suitable for labels and BLE UI."""
    configured = os.getenv("MEDICAM_DEVICE_ID", "").strip()
    if configured:
        source = configured
    else:
        source = ""
        for path in MACHINE_ID_PATHS:
            try:
                source = path.read_text(encoding="utf-8").strip()
            except OSError:
                continue
            if source:
                break
        source = source or socket.gethostname()

    digest = hashlib.sha256(f"medicam:{source}".encode("utf-8")).hexdigest()
    return digest[:8].upper()


def get_device_name() -> str:
    return f"Medicam-{get_device_id()[-6:]}"


def start_ble_recovery(duration_seconds: int = DEFAULT_BLE_RECOVERY_SECONDS) -> str:
    duration = max(60, min(int(duration_seconds), MAX_BLE_RECOVERY_SECONDS))
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=duration)
    _update_provision_fields(
        {"ble_recovery_until": expires_at.isoformat()},
        remove=(),
    )
    return expires_at.isoformat()


def stop_ble_recovery():
    _update_provision_fields({}, remove=("ble_recovery_until",))


def get_ble_recovery_until() -> str:
    data = _read_provision_data()
    value = data.get("ble_recovery_until", "")
    return value if isinstance(value, str) else ""


def is_ble_recovery_active(now: datetime | None = None) -> bool:
    value = get_ble_recovery_until()
    if not value:
        return False
    try:
        expires_at = datetime.fromisoformat(value)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        current = now or datetime.now(timezone.utc)
        return expires_at > current
    except ValueError:
        return False


def is_boot_pairing_window_active(
    window_seconds: int = BOOT_PAIRING_WINDOW_SECONDS,
) -> bool:
    """Keep BLE available briefly after a physical power cycle for recovery."""
    if os.getenv("MEDICAM_DISABLE_BOOT_PAIRING", "").lower() in {"1", "true", "yes"}:
        return False
    try:
        uptime_seconds = float(Path("/proc/uptime").read_text().split()[0])
        return uptime_seconds <= max(0, window_seconds)
    except (OSError, ValueError, IndexError):
        return False


def _read_provision_data() -> dict:
    path = _provision_path()
    try:
        if not path.exists():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _update_provision_fields(fields: dict, remove: tuple[str, ...]):
    path = _provision_path()
    data = _read_provision_data()
    data.update(fields)
    for key in remove:
        data.pop(key, None)

    tmp_path = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp_path.write_text(json.dumps(data), encoding="utf-8")
        os.replace(tmp_path, path)
        _normalize_provision_file_permissions(path)
    finally:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass

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
