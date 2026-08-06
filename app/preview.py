"""Resource-isolated MJPEG preview for the mobile application.

The recorder always owns the camera while a FullHD capture is active. Preview
work therefore reads the growing raw MJPEG recording without decoding, scaling,
or re-encoding it. A slow client can only miss preview frames: the latest JPEG
replaces the previous one and no client-facing queue can back-pressure capture.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
from typing import BinaryIO, Callable


PREVIEW_WIDTH = 640
PREVIEW_HEIGHT = 360
PREVIEW_IDLE_FPS = 10
PREVIEW_OUTPUT_FPS = 10
PREVIEW_MAX_JPEG_BYTES = 8 * 1024 * 1024
PREVIEW_READ_SIZE = 256 * 1024
PREVIEW_TAIL_POLL_SECONDS = 0.005
PREVIEW_PROCESS_STOP_TIMEOUT = 2.0


def _environment_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# This remains a deployment switch rather than a customer setting. Preview is
# temporarily disabled for the current hardware deployment while the complete
# optimized implementation and the application's preview window stay in place.
PREVIEW_ENABLED = _environment_flag("MEDICAM_PREVIEW_ENABLED", False)


def _idle_capture_command(camera_device: str) -> list[str]:
    return [
        "nice", "-n", "19",
        "v4l2-ctl",
        "-d", camera_device,
        (
            f"--set-fmt-video=width={PREVIEW_WIDTH},height={PREVIEW_HEIGHT},"
            "pixelformat=MJPG"
        ),
        f"--set-parm={PREVIEW_IDLE_FPS}",
        "--stream-mmap=4",
        "--stream-to=-",
        "--stream-count=0",
    ]


def _extract_mjpeg_frames(
    buffer: bytearray,
    should_copy: Callable[[], bool] | None = None,
):
    """Yield selected complete JPEGs while bounding malformed-stream memory.

    ``should_copy`` is evaluated once per complete image. Rejected frames are
    removed without first allocating a ``bytes`` copy, which matters when
    selecting preview frames from the growing FullHD recording.
    """
    while True:
        start = buffer.find(b"\xff\xd8")
        if start < 0:
            if len(buffer) > 1:
                del buffer[:-1]
            return
        if start:
            del buffer[:start]
        end = buffer.find(b"\xff\xd9", 2)
        if end < 0:
            if len(buffer) > PREVIEW_MAX_JPEG_BYTES:
                del buffer[:-1]
            return
        frame_end = end + 2
        copy_frame = should_copy is None or should_copy()
        frame = bytes(buffer[:frame_end]) if copy_frame else None
        del buffer[:frame_end]
        if frame is not None and len(frame) <= PREVIEW_MAX_JPEG_BYTES:
            yield frame


def _should_publish_frame(frame_index: int, source_fps: float) -> bool:
    """Evenly select at most PREVIEW_OUTPUT_FPS frames, including frame one."""
    source_rate = max(1, int(round(source_fps)))
    if source_rate <= PREVIEW_OUTPUT_FPS:
        return True
    current_slot = ((frame_index - 1) * PREVIEW_OUTPUT_FPS) // source_rate
    previous_slot = ((frame_index - 2) * PREVIEW_OUTPUT_FPS) // source_rate
    return current_slot != previous_slot


def _read_mjpeg_stream(
    stream: BinaryIO,
    stop_event: threading.Event,
    on_frame: Callable[[bytes], None],
) -> None:
    buffer = bytearray()
    while not stop_event.is_set():
        chunk = stream.read(PREVIEW_READ_SIZE)
        if not chunk:
            return
        buffer.extend(chunk)
        for frame in _extract_mjpeg_frames(buffer):
            on_frame(frame)


def _stop_process(process) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.send_signal(signal.SIGINT)
        process.wait(timeout=PREVIEW_PROCESS_STOP_TIMEOUT)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.terminate()
        process.wait(timeout=PREVIEW_PROCESS_STOP_TIMEOUT)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=PREVIEW_PROCESS_STOP_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        pass


class PreviewManager:
    def __init__(self, enabled: bool = PREVIEW_ENABLED):
        self.enabled = enabled
        self._control_lock = threading.RLock()
        self._frame_condition = threading.Condition()
        self._latest_frame: bytes | None = None
        self._frame_generation = 0
        self._producer_serial = 0
        self._producer_thread: threading.Thread | None = None
        self._producer_process = None
        self._producer_stop: threading.Event | None = None
        self._producer_kind: str | None = None
        self._subscribers = 0
        self._pause_depth = 0
        self._camera_device: str | None = None
        self._recording_raw_file: str | None = None
        self._recording_fps = 30.0
        self._recording_prepared = False
        self._recording_ready = False
        self._last_error: str | None = None

    def _invalidate_frames_locked(self) -> None:
        with self._frame_condition:
            self._latest_frame = None
            self._frame_generation += 1
            self._frame_condition.notify_all()

    def _publish(self, serial: int, frame: bytes) -> None:
        if not frame or len(frame) > PREVIEW_MAX_JPEG_BYTES:
            return
        with self._frame_condition:
            if serial != self._producer_serial:
                return
            self._latest_frame = frame
            self._frame_generation += 1
            self._frame_condition.notify_all()

    def _producer_finished(self, serial: int, error: Exception | None) -> None:
        with self._frame_condition:
            if serial != self._producer_serial:
                return
            if error is not None:
                self._last_error = f"{type(error).__name__}: {error}"
            self._frame_condition.notify_all()

    def _run_idle(self, serial: int, stop_event: threading.Event) -> None:
        process = None
        error = None
        try:
            if not self._camera_device:
                raise RuntimeError("camera_unavailable")
            process = subprocess.Popen(
                _idle_capture_command(self._camera_device),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
            self._producer_process = process
            if stop_event.is_set():
                return
            if process.stdout is None:
                raise RuntimeError("preview_stdout_unavailable")

            # Direct V4L2 mmap avoids FFmpeg's demux/mux overhead. v4l2-ctl
            # writes a short textual frame-rate confirmation before the first
            # JPEG; the SOI-aware parser safely discards that prefix.
            _read_mjpeg_stream(
                process.stdout,
                stop_event,
                lambda frame: self._publish(serial, frame),
            )
            if not stop_event.is_set() and process.poll() not in (0, None):
                raise RuntimeError(f"idle_preview_exited_{process.poll()}")
        except Exception as caught:  # Preview failure must never reach capture.
            error = caught
        finally:
            _stop_process(process)
            self._producer_finished(serial, error)

    def _run_recording(self, serial: int, stop_event: threading.Event) -> None:
        error = None
        input_buffer = bytearray()
        frame_index = 0
        try:
            raw_file = self._recording_raw_file
            if not raw_file:
                raise RuntimeError("recording_preview_source_unavailable")
            # Start at the current end so a client joining mid-recording does
            # not cause a burst of old FullHD frames to be transmitted. Frames
            # stay compressed: the iPhone performs the display downscale.
            with open(raw_file, "rb", buffering=0) as source:
                source.seek(0, os.SEEK_END)
                while not stop_event.is_set():
                    chunk = source.read(PREVIEW_READ_SIZE)
                    if not chunk:
                        time.sleep(PREVIEW_TAIL_POLL_SECONDS)
                        continue
                    input_buffer.extend(chunk)

                    def select_frame() -> bool:
                        nonlocal frame_index
                        frame_index += 1
                        return _should_publish_frame(
                            frame_index,
                            self._recording_fps,
                        )

                    for frame in _extract_mjpeg_frames(
                        input_buffer,
                        should_copy=select_frame,
                    ):
                        self._publish(serial, frame)
        except Exception as caught:  # Preview failure must never reach capture.
            error = caught
        finally:
            self._producer_finished(serial, error)

    def _stop_producer_locked(self) -> None:
        self._producer_serial += 1
        stop_event = self._producer_stop
        process = self._producer_process
        thread = self._producer_thread
        if stop_event is not None:
            stop_event.set()
        _stop_process(process)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=PREVIEW_PROCESS_STOP_TIMEOUT * 2)
        self._producer_thread = None
        self._producer_process = None
        self._producer_stop = None
        self._producer_kind = None
        self._invalidate_frames_locked()

    def _ensure_running_locked(self) -> None:
        if (
            not self.enabled
            or self._subscribers <= 0
            or self._pause_depth > 0
            or self._recording_prepared and not self._recording_ready
        ):
            return
        if self._producer_thread is not None and self._producer_thread.is_alive():
            return

        kind = "recording" if self._recording_ready else "idle"
        if kind == "recording" and not self._recording_raw_file:
            return
        if kind == "idle" and not self._camera_device:
            return
        self._stop_producer_locked()
        self._producer_serial += 1
        serial = self._producer_serial
        stop_event = threading.Event()
        target = self._run_recording if kind == "recording" else self._run_idle
        thread = threading.Thread(
            target=target,
            args=(serial, stop_event),
            name=f"medicam-preview-{kind}-{serial}",
            daemon=True,
        )
        self._producer_stop = stop_event
        self._producer_thread = thread
        self._producer_kind = kind
        self._last_error = None
        thread.start()

    def subscribe(
        self,
        camera_device: str | None = None,
        recording_raw_file: str | None = None,
        recording_fps: float = 30.0,
    ) -> int:
        with self._control_lock:
            self._subscribers += 1
            if camera_device:
                self._camera_device = camera_device
            if recording_raw_file:
                self._recording_raw_file = recording_raw_file
                self._recording_fps = recording_fps
                self._recording_prepared = True
                self._recording_ready = True
            self._ensure_running_locked()
            with self._frame_condition:
                return self._frame_generation

    def unsubscribe(self) -> None:
        with self._control_lock:
            self._subscribers = max(0, self._subscribers - 1)
            if self._subscribers == 0:
                self._stop_producer_locked()

    def ensure_running(self, camera_device: str | None = None) -> None:
        with self._control_lock:
            if camera_device:
                self._camera_device = camera_device
            self._ensure_running_locked()

    def prepare_for_recording(
        self,
        camera_device: str,
        raw_file: str,
        fps: float,
    ) -> None:
        with self._control_lock:
            self._recording_prepared = True
            self._recording_ready = False
            self._camera_device = camera_device
            self._recording_raw_file = raw_file
            self._recording_fps = fps
            self._stop_producer_locked()

    def recording_started(self) -> None:
        with self._control_lock:
            self._recording_prepared = True
            self._recording_ready = True
            self._ensure_running_locked()

    def recording_stopped(self) -> None:
        """Stop disk-tail work but defer idle capture until finalization ends."""
        with self._control_lock:
            self._recording_ready = False
            self._recording_prepared = True
            self._stop_producer_locked()

    def recording_finished(self) -> None:
        with self._control_lock:
            self._recording_ready = False
            self._recording_prepared = False
            self._recording_raw_file = None
            self._ensure_running_locked()

    def pause(self) -> None:
        with self._control_lock:
            self._pause_depth += 1
            self._stop_producer_locked()

    def resume(self) -> None:
        with self._control_lock:
            self._pause_depth = max(0, self._pause_depth - 1)
            self._ensure_running_locked()

    def wait_for_frame(
        self,
        after_generation: int,
        timeout: float = 2.0,
    ) -> tuple[int, bytes | None]:
        deadline = time.monotonic() + timeout
        with self._frame_condition:
            while self._frame_generation <= after_generation or self._latest_frame is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return self._frame_generation, None
                self._frame_condition.wait(remaining)
            return self._frame_generation, self._latest_frame

    def status(self) -> dict:
        with self._control_lock:
            thread = self._producer_thread
            return {
                "enabled": self.enabled,
                "active": bool(thread and thread.is_alive()),
                "mode": self._producer_kind or "stopped",
                "subscribers": self._subscribers,
                "width": PREVIEW_WIDTH,
                "height": PREVIEW_HEIGHT,
                "fps": PREVIEW_OUTPUT_FPS,
                "format": "mjpeg",
                "transport": "compressed_passthrough",
                "last_error": self._last_error,
            }


manager = PreviewManager()


def get_status() -> dict:
    return manager.status()


def subscribe(**kwargs) -> int:
    return manager.subscribe(**kwargs)


def unsubscribe() -> None:
    manager.unsubscribe()


def ensure_running(camera_device: str | None = None) -> None:
    manager.ensure_running(camera_device)


def wait_for_frame(after_generation: int, timeout: float = 2.0):
    return manager.wait_for_frame(after_generation, timeout)


def prepare_for_recording(camera_device: str, raw_file: str, fps: float) -> None:
    manager.prepare_for_recording(camera_device, raw_file, fps)


def recording_started() -> None:
    manager.recording_started()


def recording_stopped() -> None:
    manager.recording_stopped()


def recording_finished() -> None:
    manager.recording_finished()


def pause() -> None:
    manager.pause()


def resume() -> None:
    manager.resume()
