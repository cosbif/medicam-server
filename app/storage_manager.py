"""Storage health, retention policies, and safe media cleanup."""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app import utils


GIB = 1024 ** 3
MIB = 1024 ** 2
POLICY_FILE = os.environ.get("MEDICAM_STORAGE_POLICY_FILE", "storage_policy.json")
CRITICAL_FREE_BYTES = int(
    os.environ.get("MEDICAM_MIN_RECORDING_FREE_BYTES", 1 * GIB)
)
WARNING_FREE_BYTES = max(
    CRITICAL_FREE_BYTES,
    int(os.environ.get("MEDICAM_STORAGE_WARNING_FREE_BYTES", 5 * GIB)),
)
DEFAULT_RECORDING_BYTES_PER_SECOND = int(
    os.environ.get("MEDICAM_RECORDING_BYTES_PER_SECOND", 10 * MIB)
)
FINALIZATION_SPACE_MULTIPLIER = 2.0
MAX_LARGEST_FILES = 10
POLICY_MODES = {"off", "low_space", "keep_last_gb", "keep_last_days"}
DEFAULT_POLICY = {"mode": "off", "value": None}

_POLICY_LOCK = threading.RLock()
_CLEANUP_LOCK = threading.RLock()


class StoragePolicyError(ValueError):
    """Raised when a retention policy cannot be normalized safely."""


def _policy_path() -> Path:
    return Path(POLICY_FILE)


def _normalize_policy(mode: object, value: object = None) -> dict:
    normalized_mode = str(mode or "off").strip().lower()
    if normalized_mode not in POLICY_MODES:
        raise StoragePolicyError("unsupported_storage_policy")

    if normalized_mode in {"off", "low_space"}:
        return {"mode": normalized_mode, "value": None}

    try:
        numeric_value = float(value)
    except (TypeError, ValueError) as error:
        raise StoragePolicyError("storage_policy_value_required") from error

    if normalized_mode == "keep_last_gb":
        if not 1 <= numeric_value <= 4096:
            raise StoragePolicyError("storage_policy_value_out_of_range")
        return {"mode": normalized_mode, "value": round(numeric_value, 2)}

    if not numeric_value.is_integer() or not 1 <= numeric_value <= 3650:
        raise StoragePolicyError("storage_policy_value_out_of_range")
    return {"mode": normalized_mode, "value": int(numeric_value)}


def get_policy() -> dict:
    with _POLICY_LOCK:
        try:
            payload = json.loads(_policy_path().read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise StoragePolicyError("invalid_storage_policy")
            return _normalize_policy(payload.get("mode"), payload.get("value"))
        except FileNotFoundError:
            return dict(DEFAULT_POLICY)
        except (OSError, json.JSONDecodeError, StoragePolicyError):
            # A damaged settings file must never crash recording or permit an
            # unexpected deletion. Falling back to "off" is fail-safe.
            return dict(DEFAULT_POLICY)


def update_policy(mode: object, value: object = None) -> dict:
    policy = _normalize_policy(mode, value)
    with _POLICY_LOCK:
        path = _policy_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f"{path.name}.tmp")
        with open(temporary, "w", encoding="utf-8") as output:
            json.dump(policy, output, ensure_ascii=False, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    return dict(policy)


def _disk_usage():
    os.makedirs(utils.VIDEOS_DIR, exist_ok=True)
    return shutil.disk_usage(utils.VIDEOS_DIR)


def _library_snapshot() -> list[dict]:
    return utils.scan_video_library()


def _recording_rate(videos: list[dict]) -> tuple[float, str, int]:
    measured_bytes = 0
    measured_seconds = 0.0
    measured_files = 0
    for video in videos:
        try:
            duration = float(video.get("duration") or 0)
            size = int(video.get("size_bytes") or 0)
        except (TypeError, ValueError):
            continue
        if duration >= 1 and size > 0 and video.get("metadata_status") == "ready":
            measured_bytes += size
            measured_seconds += duration
            measured_files += 1

    if measured_seconds > 0:
        # Add a small safety margin for scenes that compress less efficiently
        # than the existing recordings.
        return measured_bytes / measured_seconds * 1.1, "measured", measured_files
    return float(DEFAULT_RECORDING_BYTES_PER_SECOND), "fallback", 0


def _estimated_minutes(free_bytes: int, bytes_per_second: float) -> float:
    usable = max(0, free_bytes - CRITICAL_FREE_BYTES)
    if bytes_per_second <= 0:
        return 0.0
    seconds = usable / (bytes_per_second * FINALIZATION_SPACE_MULTIPLIER)
    return round(seconds / 60, 1)


def get_storage_info() -> dict:
    disk = _disk_usage()
    videos = _library_snapshot()
    library_bytes = sum(int(item.get("size_bytes") or 0) for item in videos)
    rate, rate_source, rate_samples = _recording_rate(videos)
    estimated_minutes = _estimated_minutes(disk.free, rate)
    largest = sorted(
        videos,
        key=lambda item: int(item.get("size_bytes") or 0),
        reverse=True,
    )[:MAX_LARGEST_FILES]

    return {
        # Keep the original GB fields for backward-compatible app versions.
        "total": round(disk.total / GIB, 2),
        "used": round(disk.used / GIB, 2),
        "free": round(disk.free / GIB, 2),
        "total_bytes": disk.total,
        "used_bytes": disk.used,
        "free_bytes": disk.free,
        "low_space": disk.free < WARNING_FREE_BYTES,
        "critical_space": disk.free < CRITICAL_FREE_BYTES,
        "can_start_recording": disk.free >= CRITICAL_FREE_BYTES,
        "warning_free_bytes": WARNING_FREE_BYTES,
        "critical_free_bytes": CRITICAL_FREE_BYTES,
        "estimated_recording_minutes": estimated_minutes,
        "estimated_bytes_per_second": round(rate),
        "estimate_source": rate_source,
        "estimate_sample_count": rate_samples,
        "finalization_space_multiplier": FINALIZATION_SPACE_MULTIPLIER,
        "library": {
            "size_bytes": library_bytes,
            "count": len(videos),
            "largest_files": [
                {
                    "filename": item.get("filename"),
                    "size_bytes": int(item.get("size_bytes") or 0),
                    "created_at": item.get("created_at"),
                    "duration": item.get("duration"),
                }
                for item in largest
            ],
        },
        "policy": get_policy(),
    }


def _oldest_first(videos: list[dict]) -> list[dict]:
    return sorted(
        videos,
        key=lambda item: (
            int(item.get("mtime_ns") or 0),
            str(item.get("filename") or ""),
        ),
    )


def _delete_video(filename: str) -> int:
    path = utils.get_video_path(filename)
    try:
        size = os.path.getsize(path)
        os.remove(path)
    except FileNotFoundError:
        size = 0
    utils.invalidate_video_cache(filename)
    return size


def _cleanup_result(
    *,
    trigger: str,
    policy: dict | None,
    before_free: int,
    deleted: list[str],
    reclaimed_bytes: int,
    errors: list[dict],
) -> dict:
    after = _disk_usage()
    return {
        "status": "cleaned" if deleted else "unchanged",
        "trigger": trigger,
        "policy": policy,
        "deleted": deleted,
        "deleted_count": len(deleted),
        "reclaimed_bytes": reclaimed_bytes,
        "free_before_bytes": before_free,
        "free_after_bytes": after.free,
        "errors": errors,
    }


def apply_policy(
    *,
    trigger: str,
    protected_filenames: set[str] | None = None,
) -> dict:
    """Apply the persisted policy while the recorder is known to be idle."""
    policy = get_policy()
    protected = protected_filenames or set()
    with _CLEANUP_LOCK:
        before = _disk_usage()
        videos = _oldest_first(_library_snapshot())
        candidates = [
            item
            for item in videos
            if str(item.get("filename") or "") not in protected
        ]
        deleted: list[str] = []
        reclaimed = 0
        errors: list[dict] = []
        mode = policy["mode"]

        if mode == "off":
            return _cleanup_result(
                trigger=trigger,
                policy=policy,
                before_free=before.free,
                deleted=deleted,
                reclaimed_bytes=reclaimed,
                errors=errors,
            )

        library_bytes = sum(int(item.get("size_bytes") or 0) for item in videos)
        cutoff_ns = None
        if mode == "keep_last_days":
            cutoff = datetime.now(timezone.utc) - timedelta(days=policy["value"])
            cutoff_ns = int(cutoff.timestamp() * 1_000_000_000)

        for item in candidates:
            current_free = _disk_usage().free
            should_delete = False
            if mode == "low_space":
                should_delete = current_free < WARNING_FREE_BYTES
            elif mode == "keep_last_gb":
                should_delete = library_bytes > float(policy["value"]) * GIB
            elif mode == "keep_last_days":
                should_delete = int(item.get("mtime_ns") or 0) < int(cutoff_ns or 0)

            if not should_delete:
                if mode in {"low_space", "keep_last_gb"}:
                    break
                continue

            filename = str(item.get("filename") or "")
            try:
                removed = _delete_video(filename)
                reclaimed += removed
                library_bytes = max(0, library_bytes - removed)
                deleted.append(filename)
            except (OSError, ValueError) as error:
                errors.append({"filename": filename, "error": str(error)})

        return _cleanup_result(
            trigger=trigger,
            policy=policy,
            before_free=before.free,
            deleted=deleted,
            reclaimed_bytes=reclaimed,
            errors=errors,
        )


def reclaim_space(reclaim_bytes: int) -> dict:
    if reclaim_bytes <= 0:
        raise ValueError("invalid_reclaim_size")

    with _CLEANUP_LOCK:
        before = _disk_usage()
        deleted: list[str] = []
        reclaimed = 0
        errors: list[dict] = []
        for item in _oldest_first(_library_snapshot()):
            if reclaimed >= reclaim_bytes:
                break
            filename = str(item.get("filename") or "")
            try:
                removed = _delete_video(filename)
                reclaimed += removed
                deleted.append(filename)
            except (OSError, ValueError) as error:
                errors.append({"filename": filename, "error": str(error)})

        return _cleanup_result(
            trigger="manual",
            policy=None,
            before_free=before.free,
            deleted=deleted,
            reclaimed_bytes=reclaimed,
            errors=errors,
        )
