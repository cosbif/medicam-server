"""Coordinate signed OTA startup with camera hardware operations."""

from __future__ import annotations

from app import camera, diagnostics, updater


RECORDING_BLOCK_STATES = {
    "starting",
    "recording",
    "finalizing",
}


class UpdateStartBlockedError(RuntimeError):
    def __init__(self, code: str, state: str | None = None):
        super().__init__(code)
        self.code = code
        self.state = state


def start_signed_update() -> dict:
    """Atomically exclude recording startup while queuing the local updater."""
    if not diagnostics.begin_recording_start():
        raise UpdateStartBlockedError("device_busy")
    try:
        recording = camera.get_persisted_recording_status()
        recording_state = str(recording.get("state") or "unknown")[:40]
        if recording.get("capture_active") or recording_state in RECORDING_BLOCK_STATES:
            raise UpdateStartBlockedError(
                "recording_in_progress",
                recording_state,
            )
        try:
            return updater.start_update()
        except updater.UpdateBusyError as error:
            raise UpdateStartBlockedError(error.code) from error
    finally:
        diagnostics.end_recording_start()
