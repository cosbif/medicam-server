"""Consent-bound outbound WebRTC video worker.

The worker accepts only a complete SDP offer for a single receive-only video
track. Signaling uses the existing outbound cloud credential; media is sent
peer-to-peer or through authenticated TURN and is never written by this module.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from fractions import Fraction
import io
import os
import re
import time
from typing import Awaitable, Callable
import uuid

import av
from aiortc import (
    RTCConfiguration,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.mediastreams import MediaStreamError

from app import camera, preview
from app.cloud_agent import (
    CloudAgentConfig,
    CloudAgentError,
    CloudHttpClient,
    _parse_cloud_datetime,
    _read_state,
)


MAX_SDP_BYTES = 65_535
MAX_ICE_SERVERS = 4
VIDEO_WIDTH = 640
VIDEO_HEIGHT = 360
VIDEO_CLOCK_RATE = 90_000
VIDEO_FPS = 10
TURN_URL_RE = re.compile(
    r"^turns?:[A-Za-z0-9.-]+(?::[0-9]{1,5})?"
    r"(?:\?transport=(?:udp|tcp))?$"
)


class RemoteVideoError(RuntimeError):
    def __init__(self, message: str, *, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class RemoteVideoConfig:
    cloud: CloudAgentConfig
    enabled: bool = False
    source: str = "preview"
    poll_seconds: float = 2.0
    network_loss_seconds: float = 15.0

    @classmethod
    def from_environment(cls) -> "RemoteVideoConfig":
        source = os.environ.get("MEDICAM_REMOTE_VIDEO_SOURCE", "preview").strip()
        if source not in {"preview", "synthetic"}:
            raise RemoteVideoError(
                "MEDICAM_REMOTE_VIDEO_SOURCE must be preview or synthetic"
            )
        return cls(
            cloud=CloudAgentConfig.from_environment(),
            enabled=_environment_bool("MEDICAM_REMOTE_VIDEO_ENABLED", False),
            source=source,
            poll_seconds=_environment_float(
                "MEDICAM_REMOTE_VIDEO_POLL_SECONDS",
                2.0,
                minimum=1.0,
                maximum=10.0,
            ),
            network_loss_seconds=_environment_float(
                "MEDICAM_REMOTE_VIDEO_NETWORK_LOSS_SECONDS",
                15.0,
                minimum=5.0,
                maximum=60.0,
            ),
        )

    def validate(self) -> None:
        if not self.enabled:
            return
        try:
            self.cloud.validate()
        except CloudAgentError as error:
            raise RemoteVideoError(str(error)) from error


def _environment_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name, "1" if default else "0").strip().lower()
    if value in {"1", "true", "yes"}:
        return True
    if value in {"0", "false", "no"}:
        return False
    raise RemoteVideoError(f"{name} must be a boolean")


def _environment_float(
    name: str,
    default: float,
    *,
    minimum: float,
    maximum: float,
) -> float:
    try:
        value = float(os.environ.get(name, str(default)))
    except ValueError as error:
        raise RemoteVideoError(f"{name} must be numeric") from error
    if not minimum <= value <= maximum:
        raise RemoteVideoError(f"{name} must be between {minimum} and {maximum}")
    return value


def _validate_video_sdp(value: object, expected_type: str) -> str:
    if not isinstance(value, str) or not 16 <= len(value) <= MAX_SDP_BYTES:
        raise RemoteVideoError(f"invalid {expected_type} SDP length")
    if "\x00" in value or not value.startswith("v=0"):
        raise RemoteVideoError(f"invalid {expected_type} SDP")
    lines = value.replace("\r\n", "\n").splitlines()
    media_lines = [line for line in lines if line.startswith("m=")]
    if len(media_lines) != 1 or not media_lines[0].startswith("m=video "):
        raise RemoteVideoError("video session must contain exactly one video track")
    required_direction = "a=recvonly" if expected_type == "offer" else "a=sendonly"
    if required_direction not in lines:
        raise RemoteVideoError(f"{expected_type} must be {required_direction[2:]}")
    return value


def _normalize_ice_servers(payload: object, session_id: str) -> list[dict]:
    if not isinstance(payload, list) or not 1 <= len(payload) <= MAX_ICE_SERVERS:
        raise RemoteVideoError("invalid ICE server list")
    normalized = []
    for item in payload:
        if not isinstance(item, dict) or set(item) != {
            "urls",
            "username",
            "credential",
        }:
            raise RemoteVideoError("invalid ICE server fields")
        urls = item["urls"]
        username = item["username"]
        credential = item["credential"]
        if (
            not isinstance(urls, list)
            or not 1 <= len(urls) <= 4
            or any(not isinstance(url, str) or not TURN_URL_RE.fullmatch(url) for url in urls)
        ):
            raise RemoteVideoError("invalid TURN URL")
        if (
            not isinstance(username, str)
            or not re.fullmatch(r"[0-9]{10,12}:[0-9a-f-]{36}", username)
            or not username.endswith(f":{session_id}")
        ):
            raise RemoteVideoError("invalid TURN username")
        if not isinstance(credential, str) or not 20 <= len(credential) <= 128:
            raise RemoteVideoError("invalid TURN credential")
        normalized.append(
            {"urls": urls, "username": username, "credential": credential}
        )
    return normalized


def normalize_poll_response(payload: object) -> dict:
    if not isinstance(payload, dict) or set(payload) != {"session", "server_time"}:
        raise RemoteVideoError("invalid video poll response")
    _parse_cloud_datetime(payload["server_time"], "server_time")
    delivery = payload["session"]
    if delivery is None:
        return {"session": None}
    if not isinstance(delivery, dict) or set(delivery) != {
        "id",
        "state",
        "expires_at",
        "offer",
        "ice_servers",
    }:
        raise RemoteVideoError("invalid video session fields")
    session_id = delivery["id"]
    try:
        canonical_id = str(uuid.UUID(session_id))
    except (TypeError, ValueError) as error:
        raise RemoteVideoError("invalid video session ID") from error
    if session_id != canonical_id:
        raise RemoteVideoError("non-canonical video session ID")
    if delivery["state"] not in {"requested", "ready", "connected"}:
        raise RemoteVideoError("invalid video session state")
    expires_at = _parse_cloud_datetime(delivery["expires_at"], "expires_at")
    if expires_at <= datetime.now(timezone.utc):
        raise RemoteVideoError("video session has expired")
    offer = delivery["offer"]
    if not isinstance(offer, dict) or set(offer) != {"type", "sdp"}:
        raise RemoteVideoError("invalid video offer fields")
    if offer["type"] != "offer":
        raise RemoteVideoError("invalid video offer type")
    normalized_offer = _validate_video_sdp(offer["sdp"], "offer")
    return {
        "session": {
            "id": session_id,
            "state": delivery["state"],
            "expires_at": expires_at,
            "offer": normalized_offer,
            "ice_servers": _normalize_ice_servers(
                delivery["ice_servers"],
                session_id,
            ),
        }
    }


class SyntheticVideoTrack(VideoStreamTrack):
    """Deterministic moving luma pattern for Mac and CI loopback tests."""

    def __init__(self):
        super().__init__()
        self._frame_index = 0
        self._next_frame_at = time.monotonic()

    async def recv(self):
        self._next_frame_at += 1 / VIDEO_FPS
        await asyncio.sleep(max(0.0, self._next_frame_at - time.monotonic()))
        frame = av.VideoFrame(VIDEO_WIDTH, VIDEO_HEIGHT, format="yuv420p")
        luma = 32 + (self._frame_index * 5) % 180
        frame.planes[0].update(bytes([luma]) * frame.planes[0].buffer_size)
        frame.planes[1].update(bytes([96]) * frame.planes[1].buffer_size)
        frame.planes[2].update(bytes([160]) * frame.planes[2].buffer_size)
        frame.pts = self._frame_index * (VIDEO_CLOCK_RATE // VIDEO_FPS)
        frame.time_base = Fraction(1, VIDEO_CLOCK_RATE)
        self._frame_index += 1
        return frame


def _decode_preview_jpeg(encoded: bytes) -> av.VideoFrame:
    with av.open(io.BytesIO(encoded), format="mjpeg", mode="r") as container:
        frame = next(container.decode(video=0), None)
    if frame is None:
        raise RemoteVideoError("preview JPEG has no video frame")
    return frame.reformat(width=VIDEO_WIDTH, height=VIDEO_HEIGHT, format="yuv420p")


class PreviewVideoTrack(VideoStreamTrack):
    def __init__(self):
        super().__init__()
        self._generation = preview.subscribe(**camera.get_preview_source())
        self._stopped = False
        self._frame_index = 0
        self._missing_since: float | None = None

    async def recv(self):
        while not self._stopped:
            generation, encoded = await asyncio.to_thread(
                preview.wait_for_frame,
                self._generation,
                2.0,
            )
            self._generation = generation
            if encoded is None:
                preview.ensure_running(camera.find_camera_device(timeout=0.0))
                if self._missing_since is None:
                    self._missing_since = time.monotonic()
                elif time.monotonic() - self._missing_since >= 10:
                    raise MediaStreamError
                continue
            self._missing_since = None
            frame = await asyncio.to_thread(_decode_preview_jpeg, encoded)
            frame.pts = self._frame_index * (VIDEO_CLOCK_RATE // VIDEO_FPS)
            frame.time_base = Fraction(1, VIDEO_CLOCK_RATE)
            self._frame_index += 1
            return frame
        raise MediaStreamError

    def stop(self):
        if not self._stopped:
            self._stopped = True
            preview.unsubscribe()
        super().stop()


StateCallback = Callable[[str, str | None], Awaitable[None]]


class DevicePeer:
    def __init__(
        self,
        *,
        ice_servers: list[dict],
        source: str,
        state_callback: StateCallback,
    ):
        configuration = RTCConfiguration(
            iceServers=[
                RTCIceServer(
                    urls=item["urls"],
                    username=item["username"],
                    credential=item["credential"],
                )
                for item in ice_servers
            ]
        )
        self.connection = RTCPeerConnection(configuration)
        self.track = (
            SyntheticVideoTrack() if source == "synthetic" else PreviewVideoTrack()
        )
        self.state_callback = state_callback
        self._closing = False

        @self.connection.on("connectionstatechange")
        async def connection_state_changed():
            state = self.connection.connectionState
            if self._closing:
                return
            if state == "connected":
                await self.state_callback("connected", None)
            elif state == "failed":
                await self.state_callback("failed", "ice_failed")
            elif state == "closed":
                await self.state_callback("closed", None)

    async def negotiate(self, offer_sdp: str) -> str:
        _validate_video_sdp(offer_sdp, "offer")
        await self.connection.setRemoteDescription(
            RTCSessionDescription(sdp=offer_sdp, type="offer")
        )
        self.connection.addTrack(self.track)
        answer = await self.connection.createAnswer()
        await self.connection.setLocalDescription(answer)
        local = self.connection.localDescription
        if local is None:
            raise RemoteVideoError("WebRTC answer was not created")
        return _validate_video_sdp(local.sdp, "answer")

    async def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        self.track.stop()
        await self.connection.close()


class RemoteVideoService:
    def __init__(
        self,
        config: RemoteVideoConfig,
        *,
        client: CloudHttpClient | None = None,
    ):
        config.validate()
        self.config = config
        self.client = client or CloudHttpClient(config.cloud)
        self.stop_event = asyncio.Event()
        self.active_session_id: str | None = None
        self.active_peer: DevicePeer | None = None
        self._pending_terminal: tuple[str, str, str | None] | None = None
        self._last_cloud_success = time.monotonic()

    def _device_token(self) -> str:
        state = _read_state(self.config.cloud.state_file)
        if state.get("device_id") != self.config.cloud.device_id:
            raise RemoteVideoError("cloud state belongs to another device")
        if state.get("server_url") != self.config.cloud.server_url:
            raise RemoteVideoError("cloud state belongs to another server")
        token = state.get("device_token")
        if not isinstance(token, str) or not 32 <= len(token) <= 128:
            raise RemoteVideoError("cloud device credential is unavailable")
        return token

    async def _post(self, path: str, payload: dict, token: str) -> dict:
        try:
            return await asyncio.to_thread(self.client.post, path, payload, token)
        except CloudAgentError as error:
            raise RemoteVideoError(
                str(error),
                status_code=error.status_code,
            ) from error

    async def _report_state(
        self,
        session_id: str,
        state: str,
        error_code: str | None,
        token: str,
    ) -> None:
        payload = {"state": state}
        if error_code is not None:
            payload["error_code"] = error_code
        await self._post(
            f"/api/v1/device/video/sessions/{session_id}/state",
            payload,
            token,
        )

    async def _peer_state_changed(self, state: str, error_code: str | None) -> None:
        session_id = self.active_session_id
        if session_id is None:
            return
        try:
            token = self._device_token()
            await self._report_state(session_id, state, error_code, token)
        except RemoteVideoError:
            self._pending_terminal = (session_id, state, error_code)
        if state in {"closed", "failed"}:
            await self._close_active()

    async def _close_active(self) -> None:
        peer = self.active_peer
        self.active_peer = None
        self.active_session_id = None
        if peer is not None:
            await peer.close()

    async def _flush_pending_terminal(self, token: str) -> None:
        pending = self._pending_terminal
        if pending is None:
            return
        try:
            await self._report_state(*pending, token)
        except RemoteVideoError as error:
            # The viewer can close or revoke the cloud session before the
            # device observes its peer closing. The terminal state is already
            # durable in that case, so a retryable local report must not block
            # every subsequent video-session poll forever.
            if error.status_code not in {404, 409}:
                raise
        self._pending_terminal = None

    async def _start_session(self, delivery: dict, token: str) -> None:
        session_id = delivery["id"]
        if self.active_session_id == session_id:
            return
        await self._close_active()
        peer = DevicePeer(
            ice_servers=delivery["ice_servers"],
            source=self.config.source,
            state_callback=self._peer_state_changed,
        )
        self.active_session_id = session_id
        self.active_peer = peer
        try:
            answer_sdp = await peer.negotiate(delivery["offer"])
            await self._post(
                f"/api/v1/device/video/sessions/{session_id}/answer",
                {"answer": {"type": "answer", "sdp": answer_sdp}},
                token,
            )
        except Exception as error:
            error_code = (
                "camera_unavailable"
                if isinstance(error, (MediaStreamError, FileNotFoundError))
                else "negotiation_failed"
            )
            try:
                await self._report_state(session_id, "failed", error_code, token)
            except RemoteVideoError:
                self._pending_terminal = (session_id, "failed", error_code)
            await self._close_active()

    async def _handle_poll(self, payload: object, token: str) -> None:
        normalized = normalize_poll_response(payload)
        delivery = normalized["session"]
        if delivery is None:
            await self._close_active()
            return
        await self._start_session(delivery, token)

    async def run(self) -> None:
        if not self.config.enabled:
            return
        failures = 0
        while not self.stop_event.is_set():
            try:
                token = self._device_token()
                await self._flush_pending_terminal(token)
                response = await self._post(
                    "/api/v1/device/video/sessions/poll",
                    {},
                    token,
                )
                await self._handle_poll(response, token)
                self._last_cloud_success = time.monotonic()
                failures = 0
                delay = self.config.poll_seconds
            except (RemoteVideoError, OSError) as error:
                failures += 1
                delay = min(self.config.poll_seconds * (2 ** min(failures - 1, 3)), 15)
                if (
                    self.active_session_id is not None
                    and time.monotonic() - self._last_cloud_success
                    >= self.config.network_loss_seconds
                ):
                    self._pending_terminal = (
                        self.active_session_id,
                        "failed",
                        "network_lost",
                    )
                    await self._close_active()
                print(f"remote video poll failed: {error}; retry={delay}s", flush=True)
            try:
                await asyncio.wait_for(self.stop_event.wait(), timeout=delay)
            except TimeoutError:
                pass

    async def stop(self) -> None:
        self.stop_event.set()
        await self._close_active()
