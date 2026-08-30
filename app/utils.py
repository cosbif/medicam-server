import os
import re
import base64
import hmac
import secrets
import subprocess
import hashlib
import socket
import ssl
import threading
import fcntl
import pwd
import stat
from contextlib import contextmanager
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
BLE_PROVISIONED_STATE_FILE = Path(
    os.environ.get(
        "MEDICAM_BLE_PROVISIONED_STATE_FILE",
        "/var/lib/medicam/ble-provisioned.state",
    )
)
BLE_RECOVERY_STATE_FILE = Path(
    os.environ.get(
        "MEDICAM_BLE_RECOVERY_STATE_FILE",
        "/var/lib/medicam/ble-recovery-until.state",
    )
)
BLE_REFRESH_REQUEST_FILE = Path(
    os.environ.get(
        "MEDICAM_BLE_REFRESH_REQUEST_FILE",
        "/var/lib/medicam/ble-refresh.request",
    )
)
POWER_OFF_REQUEST_FILE = Path(
    os.environ.get(
        "MEDICAM_POWER_OFF_REQUEST_FILE",
        "/var/lib/medicam/poweroff.request",
    )
)
MACHINE_ID_PATHS = (Path("/etc/machine-id"), Path("/var/lib/dbus/machine-id"))
PAIRING_SECRET_FILE = Path("/etc/medicam/pairing-secret")
TLS_CERT_FILE = Path("/etc/medicam/tls/cert.pem")
PAIRING_CLIENT_CONTEXT = "medicam-client-v1"
PAIRING_SERVER_CONTEXT = "medicam-server-v1"
PAIRING_SESSION_CONTEXT = "medicam-session-v1"
OWNER_PAIRING_CLIENT_CONTEXT = "medicam-owner-client-v1"
OWNER_PAIRING_SERVER_CONTEXT = "medicam-owner-server-v1"
OWNER_PAIRING_SESSION_CONTEXT = "medicam-owner-session-v1"
OWNER_API_TOKEN_CONTEXT = "medicam-owner-token-v1"

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
        # nmcli localizes human-readable state values.  The BLE manager runs
        # under systemd and may therefore inherit a different locale from an
        # interactive SSH session.  Force the stable machine-readable English
        # value so a connected camera is never mistaken for an offline one.
        nmcli_environment = os.environ.copy()
        nmcli_environment.update({"LANG": "C", "LC_ALL": "C"})
        status = subprocess.check_output(
            ["nmcli", "-t", "-f", "TYPE,STATE", "dev", "status"],
            text=True,
            encoding="utf-8",
            errors="ignore",
            timeout=5,
            env=nmcli_environment,
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
            timeout=5,
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
            timeout=5,
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
    if is_wifi_connected() and get_wifi_ssid() == ssid:
        return {
            "ok": True,
            "stdout": "already_connected",
            "stderr": "",
            "error_code": "",
            "ip": get_primary_ipv4(),
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
    configured = os.getenv("MEDICAM_PROVISION_FILE", "").strip()
    if configured:
        return Path(configured)
    # Development/test fallback. Production systemd always points at
    # /var/lib/medicam/provision.json.
    project_root = Path(__file__).resolve().parents[1]
    return project_root / PROVISION_FILENAME


def _provision_lock_path() -> Path:
    configured = os.getenv("MEDICAM_PROVISION_LOCK_FILE", "").strip()
    if configured:
        return Path(configured)
    path = _provision_path()
    return path.with_name(f".{path.name}.lock")


def _radxa_ids() -> tuple[int, int] | None:
    try:
        account = pwd.getpwnam("radxa")
        return account.pw_uid, account.pw_gid
    except KeyError:
        return None


@contextmanager
def _provision_lock(*, exclusive: bool):
    """Cross-process lock that cannot be redirected through a symlink."""
    path = _provision_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            owner = _radxa_ids()
            if owner is not None:
                os.fchown(descriptor, *owner)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("provision_lock_not_regular")
        fcntl.flock(
            descriptor,
            fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH,
        )
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _open_provision_directory(path: Path) -> int:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return os.open(path.parent, flags)


def _read_provision_data_unlocked(path: Path) -> dict:
    try:
        directory = _open_provision_directory(path)
    except OSError:
        return {}
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path.name, flags, dir_fd=directory)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return {}
            # Devices upgraded from early Medicam releases may still have a
            # group/world-readable provision.json. It contains the owner API
            # token, so tighten inherited permissions through the already
            # verified, O_NOFOLLOW file descriptor before reading any data.
            if metadata.st_mode & 0o077:
                os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as value:
                data = json.load(value)
            return data if isinstance(data, dict) else {}
        finally:
            os.close(descriptor)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    finally:
        os.close(directory)


def _atomic_write_provision_data_unlocked(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = _open_provision_directory(path)
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
        os.fchmod(descriptor, 0o600)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            owner = _radxa_ids()
            if owner is not None:
                os.fchown(descriptor, *owner)
        payload = json.dumps(data, ensure_ascii=False, sort_keys=True)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _write_ble_provisioned_state(value: bool) -> None:
    """Publish only the non-secret ownership bit for the root BLE manager."""
    path = BLE_PROVISIONED_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = _open_provision_directory(path)
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
        os.write(descriptor, b"1\n" if value else b"0\n")
        os.fsync(descriptor)
        # This marker contains no token, SSID, or customer data.  It is
        # intentionally readable while provision.json remains mode 0600.
        os.fchmod(descriptor, 0o644)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _sync_ble_provisioned_state(value: bool) -> None:
    try:
        _write_ble_provisioned_state(value)
    except OSError:
        # A missing marker safely keeps BLE available. Ownership state and
        # API authentication continue to use the private provision.json.
        pass


def _write_ble_recovery_state(expires_at: str) -> None:
    """Publish only the non-secret recovery deadline for the root manager."""
    path = BLE_RECOVERY_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = _open_provision_directory(path)
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(temporary_name, flags, 0o600, dir_fd=directory)
        payload = f"{expires_at}\n".encode("ascii")
        os.write(descriptor, payload)
        os.fsync(descriptor)
        # A UTC deadline contains no credentials or customer network data.
        os.fchmod(descriptor, 0o644)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def _sync_ble_recovery_state(expires_at: str) -> None:
    try:
        _write_ble_recovery_state(expires_at)
    except (OSError, UnicodeEncodeError):
        # The private state remains authoritative. A stale public deadline can
        # only keep BLE available until its already bounded expiry.
        pass


def get_ble_recovery_state_until() -> str:
    """Read the public, credential-free recovery deadline without symlinks."""
    path = BLE_RECOVERY_STATE_FILE
    try:
        directory = _open_provision_directory(path)
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path.name, flags, dir_fd=directory)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022:
                    raise OSError("unsafe_ble_recovery_state")
                value = os.read(descriptor, 128).decode("ascii").strip()
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
        return value
    except (OSError, UnicodeDecodeError):
        return ""


def is_ble_provisioned() -> bool:
    """Read the non-secret marker used by the capability-free BLE manager."""
    path = BLE_PROVISIONED_STATE_FILE
    try:
        directory = _open_provision_directory(path)
        try:
            flags = os.O_RDONLY
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            descriptor = os.open(path.name, flags, dir_fd=directory)
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o022:
                    raise OSError("unsafe_ble_provisioned_state")
                value = os.read(descriptor, 16).decode("ascii").strip()
            finally:
                os.close(descriptor)
        finally:
            os.close(directory)
        if value == "1":
            return True
        if value == "0":
            return False
    except (OSError, UnicodeDecodeError):
        pass
    # Fail open for physical recovery when the marker is absent or invalid.
    return is_provisioned()


def is_provisioned() -> bool:
    """Возвращает True если устройство provisioned (подключено к Wi-Fi и помечено)."""
    data = _read_provision_data()
    value = bool(data.get("provisioned", False))
    if "provisioned" in data:
        _sync_ble_provisioned_state(value)
    return value

def set_provisioned(
    value: bool,
    info: dict | None = None,
    *,
    api_token: str | None = None,
):
    """Записывает статус provisioned и доп.инфо (ssid, ip, timestamp)."""
    path = _provision_path()
    if value and api_token is not None and not is_valid_api_token(api_token):
        raise ValueError("invalid_api_token_format")
    with _provision_lock(exclusive=True):
        data = _read_provision_data_unlocked(path)
        data["provisioned"] = bool(value)
        if info is not None and value:
            data.setdefault("info", {}).update(info)
        elif info is not None:
            data["info"] = dict(info)
        if value:
            data["api_token"] = api_token or data.get("api_token") or generate_api_token()
        else:
            data.pop("api_token", None)
        data.pop("ble_recovery_until", None)
        data.setdefault("info", {})["updated_at"] = datetime.now().isoformat()
        _atomic_write_provision_data_unlocked(path, data)
    _sync_ble_provisioned_state(bool(value))
    _sync_ble_recovery_state("")


def _normalize_provision_file_permissions(path: Path):
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("provision_file_not_regular")
        os.fchmod(descriptor, 0o600)
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            owner = _radxa_ids()
            if owner is not None:
                os.fchown(descriptor, *owner)
    finally:
        os.close(descriptor)


def generate_api_token() -> str:
    return secrets.token_urlsafe(API_TOKEN_BYTES)


def is_valid_api_token(token: str | None) -> bool:
    return bool(
        isinstance(token, str)
        and 32 <= len(token) <= 128
        and re.fullmatch(r"[A-Za-z0-9_-]+", token)
    )


def derive_owner_api_token(
    session_key_hex: str,
    session_id: str,
    device_id: str,
) -> str:
    """Derive the next owner token without transmitting it over Bluetooth."""
    try:
        session_key = bytes.fromhex(session_key_hex)
    except (TypeError, ValueError):
        session_key = b""
    if (
        len(session_key) != 32
        or not isinstance(session_id, str)
        or not session_id
        or not isinstance(device_id, str)
        or not device_id
    ):
        raise ValueError("invalid_pairing_session")
    message = "\0".join(
        (OWNER_API_TOKEN_CONTEXT, session_id, device_id)
    ).encode("utf-8")
    digest = hmac.new(session_key, message, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def get_api_token() -> str:
    token = _read_provision_data().get("api_token", "")
    return token if isinstance(token, str) else ""


def verify_api_token(token: str | None) -> bool:
    expected = get_api_token()
    if not expected or not token:
        return False
    return hmac.compare_digest(token, expected)


def rotate_api_token(current_token: str | None, new_token: str) -> bool:
    """Atomically replace the owner token only if the current token still matches."""
    if not is_valid_api_token(new_token):
        raise ValueError("invalid_new_api_token")
    path = _provision_path()
    with _provision_lock(exclusive=True):
        data = _read_provision_data_unlocked(path)
        expected = data.get("api_token", "")
        if (
            not data.get("provisioned")
            or not isinstance(expected, str)
            or not current_token
            or not hmac.compare_digest(current_token, expected)
        ):
            return False
        if hmac.compare_digest(new_token, expected):
            raise ValueError("new_api_token_must_differ")
        data["api_token"] = new_token
        data.setdefault("info", {})["token_rotated_at"] = datetime.now(
            timezone.utc
        ).isoformat()
        _atomic_write_provision_data_unlocked(path, data)
        return True


def _pairing_secret_path() -> Path:
    return Path(
        os.getenv("MEDICAM_PAIRING_SECRET_FILE", str(PAIRING_SECRET_FILE))
    )


def _tls_cert_path() -> Path:
    return Path(os.getenv("MEDICAM_TLS_CERT_FILE", str(TLS_CERT_FILE)))


def get_pairing_secret() -> str:
    path = _pairing_secret_path()
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
            raise OSError("unsafe_pairing_secret_permissions")
        with os.fdopen(descriptor, "r", encoding="ascii", closefd=False) as value:
            secret = value.read(256).strip().replace("-", "").upper()
    finally:
        os.close(descriptor)
    if not re.fullmatch(r"[A-Z2-7]{26}", secret):
        raise OSError("invalid_pairing_secret")
    return secret


def _pairing_hmac(context: str, nonce: str, *values: str) -> str:
    secret = get_pairing_secret().encode("ascii")
    message = "\0".join((context, nonce, *values)).encode("utf-8")
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def pairing_client_proof(nonce: str, device_id: str) -> str:
    return _pairing_hmac(PAIRING_CLIENT_CONTEXT, nonce, device_id)


def pairing_server_proof(nonce: str, device_id: str, tls_fingerprint: str) -> str:
    return _pairing_hmac(
        PAIRING_SERVER_CONTEXT,
        nonce,
        device_id,
        tls_fingerprint,
    )


def pairing_session_key(nonce: str, device_id: str) -> str:
    return _pairing_hmac(PAIRING_SESSION_CONTEXT, nonce, device_id)


def verify_pairing_client_proof(nonce: str, device_id: str, proof: str) -> bool:
    try:
        expected = pairing_client_proof(nonce, device_id)
    except OSError:
        return False
    return bool(proof and hmac.compare_digest(proof, expected))


def _owner_pairing_hmac(context: str, nonce: str, *values: str) -> str:
    """Authenticate BLE recovery with the existing owner token.

    The token remains in the iPhone Keychain and in the camera provision file;
    only a nonce-bound HMAC crosses Bluetooth.
    """
    token = get_api_token()
    if not is_valid_api_token(token):
        raise OSError("owner_token_unavailable")
    message = "\0".join((context, nonce, *values)).encode("utf-8")
    return hmac.new(token.encode("ascii"), message, hashlib.sha256).hexdigest()


def owner_pairing_client_proof(nonce: str, device_id: str) -> str:
    return _owner_pairing_hmac(OWNER_PAIRING_CLIENT_CONTEXT, nonce, device_id)


def owner_pairing_server_proof(
    nonce: str,
    device_id: str,
    tls_fingerprint: str,
) -> str:
    return _owner_pairing_hmac(
        OWNER_PAIRING_SERVER_CONTEXT,
        nonce,
        device_id,
        tls_fingerprint,
    )


def owner_pairing_session_key(nonce: str, device_id: str) -> str:
    return _owner_pairing_hmac(
        OWNER_PAIRING_SESSION_CONTEXT,
        nonce,
        device_id,
    )


def verify_owner_pairing_client_proof(
    nonce: str,
    device_id: str,
    proof: str,
) -> bool:
    try:
        expected = owner_pairing_client_proof(nonce, device_id)
    except OSError:
        return False
    return bool(proof and hmac.compare_digest(proof, expected))


def get_tls_fingerprint() -> str:
    try:
        pem = _tls_cert_path().read_text(encoding="ascii")
        der = ssl.PEM_cert_to_DER_cert(pem)
        return hashlib.sha256(der).hexdigest()
    except (OSError, ValueError):
        return ""


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
    try:
        _write_ble_recovery_state(expires_at.isoformat())
    except (OSError, UnicodeEncodeError):
        _update_provision_fields({}, remove=("ble_recovery_until",))
        raise
    return expires_at.isoformat()


def _request_root_action(path: Path, action: str) -> None:
    """Atomically signal one fixed root-owned systemd.path action."""
    path.parent.mkdir(parents=True, exist_ok=True)
    directory = _open_provision_directory(path)
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii", closefd=False) as output:
            output.write(
                f"{action}-requested-at={datetime.now(timezone.utc).isoformat()}\n"
            )
            output.flush()
            os.fsync(output.fileno())
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def request_ble_refresh() -> None:
    """Atomically signal the fixed root-owned systemd.path BLE restart."""
    _request_root_action(BLE_REFRESH_REQUEST_FILE, "refresh")


def request_poweroff() -> None:
    """Atomically ask the fixed root-owned systemd.path to power off."""
    _request_root_action(POWER_OFF_REQUEST_FILE, "poweroff")


def stop_ble_recovery():
    # Publish the fail-closed state first so the manager never extends an
    # explicitly closed window because of a stale marker.
    _write_ble_recovery_state("")
    _update_provision_fields({}, remove=("ble_recovery_until",))


def get_ble_recovery_until() -> str:
    """Return the latest valid private or root-published recovery deadline."""

    data = _read_provision_data()
    private_value = data.get("ble_recovery_until", "")
    public_value = get_ble_recovery_state_until()
    deadlines: list[tuple[datetime, str]] = []
    for value in (private_value, public_value):
        if not isinstance(value, str) or not value:
            continue
        try:
            deadline = datetime.fromisoformat(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=timezone.utc)
            deadlines.append((deadline, value))
        except ValueError:
            continue
    return max(
        deadlines,
        default=(datetime.min.replace(tzinfo=timezone.utc), ""),
    )[1]


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
        with _provision_lock(exclusive=False):
            return _read_provision_data_unlocked(path)
    except OSError:
        return {}


def _update_provision_fields(fields: dict, remove: tuple[str, ...]):
    path = _provision_path()
    with _provision_lock(exclusive=True):
        data = _read_provision_data_unlocked(path)
        data.update(fields)
        for key in remove:
            data.pop(key, None)
        _atomic_write_provision_data_unlocked(path, data)

def get_provision_info() -> dict:
    """Возвращает словарь с инфо (ssid, ip и т.п.) или пустой словарь."""
    data = _read_provision_data()
    info = data.get("info", {})
    return info if isinstance(info, dict) else {}
