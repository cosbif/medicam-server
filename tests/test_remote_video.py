from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import time
import unittest
import uuid

from aiortc import RTCPeerConnection

from app.cloud_agent import CloudAgentConfig, CloudAgentError
from app.remote_video import (
    DevicePeer,
    RemoteVideoConfig,
    RemoteVideoError,
    RemoteVideoService,
    SyntheticVideoTrack,
    normalize_poll_response,
)


class RemoteVideoValidationTests(unittest.TestCase):
    def _payload(self) -> dict:
        session_id = str(uuid.uuid4())
        expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
        turn_expires = int(expires_at.timestamp())
        return {
            "session": {
                "id": session_id,
                "state": "requested",
                "expires_at": expires_at.isoformat(),
                "offer": {
                    "type": "offer",
                    "sdp": (
                        "v=0\r\n"
                        "o=- 1 1 IN IP4 127.0.0.1\r\n"
                        "s=-\r\n"
                        "t=0 0\r\n"
                        "m=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
                        "a=recvonly\r\n"
                    ),
                },
                "ice_servers": [
                    {
                        "urls": [
                            "turn:api.medicam-cloud.ru:3478?transport=udp",
                            "turn:api.medicam-cloud.ru:3478?transport=tcp",
                        ],
                        "username": f"{turn_expires}:{session_id}",
                        "credential": "c" * 28,
                    }
                ],
            },
            "server_time": datetime.now(timezone.utc).isoformat(),
        }

    def test_accepts_one_receive_only_video_track_and_turn_credentials(self):
        payload = self._payload()

        normalized = normalize_poll_response(payload)

        self.assertEqual(normalized["session"]["id"], payload["session"]["id"])
        self.assertEqual(normalized["session"]["state"], "requested")
        self.assertEqual(
            normalized["session"]["ice_servers"][0]["urls"],
            payload["session"]["ice_servers"][0]["urls"],
        )

    def test_rejects_audio_or_data_channels(self):
        payload = self._payload()
        payload["session"]["offer"]["sdp"] += (
            "m=application 9 UDP/DTLS/SCTP webrtc-datachannel\r\n"
        )

        with self.assertRaisesRegex(RemoteVideoError, "exactly one video"):
            normalize_poll_response(payload)

    def test_rejects_non_turn_ice_server(self):
        payload = self._payload()
        payload["session"]["ice_servers"][0]["urls"] = [
            "stun:api.medicam-cloud.ru:3478"
        ]

        with self.assertRaisesRegex(RemoteVideoError, "invalid TURN URL"):
            normalize_poll_response(payload)

    def test_rejects_unexpected_response_fields(self):
        payload = self._payload()
        payload["session"]["owner_override"] = True

        with self.assertRaisesRegex(RemoteVideoError, "invalid video session fields"):
            normalize_poll_response(payload)


class RemoteVideoLoopbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_synthetic_track_reaches_authenticated_webrtc_peer(self):
        viewer = RTCPeerConnection()
        device = None
        received_frame = asyncio.get_running_loop().create_future()
        state_changes = []

        @viewer.on("track")
        async def track_received(track):
            try:
                frame = await track.recv()
                if not received_frame.done():
                    received_frame.set_result(frame)
            except Exception as error:
                if not received_frame.done():
                    received_frame.set_exception(error)

        async def state_changed(state: str, error_code: str | None):
            state_changes.append((state, error_code))

        try:
            viewer.addTransceiver("video", direction="recvonly")
            offer = await viewer.createOffer()
            await viewer.setLocalDescription(offer)
            self.assertIsNotNone(viewer.localDescription)

            device = DevicePeer(
                ice_servers=[],
                source="synthetic",
                state_callback=state_changed,
            )
            answer_sdp = await device.negotiate(viewer.localDescription.sdp)
            await viewer.setRemoteDescription(
                type(viewer.localDescription)(sdp=answer_sdp, type="answer")
            )

            frame = await asyncio.wait_for(received_frame, timeout=10)
            self.assertEqual((frame.width, frame.height), (640, 360))
            self.assertIsInstance(device.track, SyntheticVideoTrack)
            await asyncio.wait_for(
                self._wait_for_state(state_changes, "connected"),
                timeout=5,
            )
        finally:
            if device is not None:
                await device.close()
            await viewer.close()

    async def _wait_for_state(self, states, expected):
        while not any(state == expected for state, _ in states):
            await asyncio.sleep(0.05)

    async def test_cloud_loss_closes_media_after_bounded_grace_period(self):
        with tempfile.TemporaryDirectory() as temporary:
            state_file = Path(temporary) / "cloud-state.json"
            state_file.write_text(
                json.dumps(
                    {
                        "device_id": "ABCD1234",
                        "server_url": "http://127.0.0.1:8000",
                        "device_token": "device-token-" + "x" * 32,
                    }
                ),
                encoding="utf-8",
            )
            state_file.chmod(0o600)
            cloud = CloudAgentConfig(
                server_url="http://127.0.0.1:8000",
                device_id="ABCD1234",
                bootstrap_token="",
                state_file=state_file,
                ca_file=None,
            )
            config = RemoteVideoConfig(
                cloud=cloud,
                enabled=True,
                source="synthetic",
                poll_seconds=1,
                network_loss_seconds=5,
            )

            class UnavailableCloud:
                def post(self, path, payload, token):
                    raise CloudAgentError("simulated cloud outage")

            class ActivePeer:
                def __init__(self):
                    self.closed = asyncio.Event()

                async def close(self):
                    self.closed.set()

            service = RemoteVideoService(config, client=UnavailableCloud())
            peer = ActivePeer()
            service.active_session_id = str(uuid.uuid4())
            service.active_peer = peer
            service._last_cloud_success = time.monotonic() - 6
            task = asyncio.create_task(service.run())
            try:
                await asyncio.wait_for(peer.closed.wait(), timeout=2)
                self.assertIsNone(service.active_session_id)
                self.assertEqual(
                    service._pending_terminal[1:],
                    ("failed", "network_lost"),
                )
            finally:
                await service.stop()
                await task
