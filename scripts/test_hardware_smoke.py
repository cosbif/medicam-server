#!/usr/bin/env python3
"""Run a destructive-safe hardware smoke test against a Medicam backend.

The test restores camera settings and removes only the recording it creates.
Pre-existing videos are never modified.
"""

import argparse
import json
import os
from pathlib import Path
import ssl
import struct
import subprocess
import sys
import time
from urllib import error, parse, request


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class SmokeFailure(RuntimeError):
    pass


class ApiClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"X-Medicam-Token": token}
        self.context = ssl._create_unverified_context()

    def open(self, method: str, path: str, *, form=None, headers=None, timeout=30):
        body = None
        merged_headers = dict(self.headers)
        if form is not None:
            body = parse.urlencode(form).encode("utf-8")
            merged_headers["Content-Type"] = "application/x-www-form-urlencoded"
        merged_headers.update(headers or {})
        api_request = request.Request(
            f"{self.base_url}{path}",
            method=method,
            data=body,
            headers=merged_headers,
        )
        try:
            return request.urlopen(
                api_request,
                timeout=timeout,
                context=self.context,
            )
        except error.HTTPError as exc:
            response_body = exc.read().decode("utf-8", errors="replace")
            raise SmokeFailure(
                f"{method} {path}: HTTP {exc.code}: {response_body}"
            ) from exc

    def json(self, method: str, path: str, *, form=None, timeout=30):
        with self.open(method, path, form=form, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))


def require(condition, message):
    if not condition:
        raise SmokeFailure(message)


def local_token():
    try:
        from app import utils

        return utils.get_api_token()
    except Exception:
        return ""


def video_entries(client, *, refresh=False):
    suffix = "?page_size=100&refresh=true" if refresh else "?page_size=100"
    return client.json("GET", f"/videos{suffix}").get("videos", [])


def wait_for_metadata(client, filename, timeout=45):
    deadline = time.monotonic() + timeout
    refreshed = False
    while time.monotonic() < deadline:
        entries = video_entries(client, refresh=not refreshed)
        refreshed = True
        entry = next(
            (item for item in entries if item.get("filename") == filename),
            None,
        )
        if entry and entry.get("metadata_status") == "ready":
            return entry
        if entry and entry.get("metadata_status") == "error":
            raise SmokeFailure(f"metadata generation failed: {entry}")
        time.sleep(1)
    raise SmokeFailure(f"metadata was not ready for {filename}")


def ffprobe(path):
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,width,height,avg_frame_rate,channels,sample_rate",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        timeout=120,
        check=False,
    )
    require(result.returncode == 0, result.stderr.strip() or "ffprobe failed")
    return json.loads(result.stdout)


def read_preview_frame(client):
    with client.open("GET", "/preview/stream", timeout=20) as response:
        length_bytes = response.read(4)
        require(len(length_bytes) == 4, "preview frame length is incomplete")
        frame_length = struct.unpack(">I", length_bytes)[0]
        require(1_000 <= frame_length <= 10 * 1024 * 1024, "invalid preview frame size")
        frame = response.read(frame_length)
        require(len(frame) == frame_length, "preview JPEG is incomplete")
        require(frame.startswith(b"\xff\xd8") and frame.endswith(b"\xff\xd9"), "preview is not JPEG")
        return frame_length


def restore_settings(client, settings):
    client.json(
        "POST",
        "/settings",
        form={
            "resolution": settings["resolution"],
            "fps": str(settings["fps"]),
            "audio_enabled": "true" if settings.get("audio_enabled") else "false",
            "audio_device": settings.get("audio_device", "auto"),
        },
    )


def run(client, duration, videos_dir):
    original_settings = client.json("GET", "/settings")
    before_entries = video_entries(client, refresh=True)
    before_names = {entry["filename"] for entry in before_entries}
    created_filename = None
    recording_started = False
    report = {
        "original_settings": original_settings,
        "pre_existing_videos": sorted(before_names),
    }

    status = client.json("GET", "/recording/status")
    require(not status.get("recording"), f"camera is busy: {status}")

    try:
        settings = client.json(
            "POST",
            "/settings",
            form={
                "resolution": "FHD",
                "fps": "30",
                "audio_enabled": "true",
                "audio_device": "auto",
            },
        )
        require(settings.get("resolution") == "FHD", f"FullHD rejected: {settings}")
        require(str(settings.get("fps")) == "30", f"30 fps rejected: {settings}")
        require(settings.get("audio_enabled") is True, f"audio rejected: {settings}")

        audio_test = client.json(
            "POST",
            "/audio/test",
            form={"duration_seconds": "2", "device": "auto"},
            timeout=20,
        )
        require(audio_test.get("signal_detected") is True, f"audio signal missing: {audio_test}")
        report["audio_test"] = {
            key: audio_test.get(key)
            for key in ("device", "rms_dbfs", "peak_dbfs", "clipped_percent", "signal_detected")
        }

        started = client.json("POST", "/start", timeout=45)
        recording_started = True
        created_filename = Path(started.get("file", "")).name or None
        require(started.get("status") == "recording_started", f"start failed: {started}")
        require(started.get("resolution") == "1920x1080", f"wrong resolution: {started}")
        require(str(started.get("fps")) == "30", f"wrong fps: {started}")

        time.sleep(3)
        active = client.json("GET", "/recording/status")
        require(active.get("state") == "recording", f"recording state failed: {active}")
        require(active.get("capture_active") is True, f"video capture failed: {active}")
        require(active.get("audio", {}).get("recording") is True, f"audio capture failed: {active}")
        report["preview_frame_bytes"] = read_preview_frame(client)

        remaining = max(0.0, duration - 3.0)
        if remaining:
            time.sleep(remaining)

        stopped = client.json("POST", "/stop", timeout=15 * 60)
        recording_started = False
        created_filename = Path(stopped.get("file", "")).name or created_filename
        require(stopped.get("status") == "recording_stopped", f"stop failed: {stopped}")
        require(stopped.get("returncode") == 0, f"finalization failed: {stopped}")

        quality = stopped.get("quality") or {}
        require(quality.get("valid") is True, f"invalid recording: {quality}")
        require(quality.get("healthy") is True, f"unhealthy recording: {quality}")
        require(quality.get("resolution") == "1920x1080", f"wrong output: {quality}")
        require(float(quality.get("avg_fps") or 0) >= 29.5, f"low fps: {quality}")
        require(float(quality.get("frame_delivery_ratio") or 0) >= 0.995, f"lost frames: {quality}")
        require(created_filename and created_filename not in before_names, "test output was not isolated")

        metadata = wait_for_metadata(client, created_filename)
        require(metadata.get("resolution") == "1920x1080", f"metadata resolution failed: {metadata}")
        require(float(metadata.get("fps") or 0) >= 29.5, f"metadata fps failed: {metadata}")
        require(metadata.get("has_audio") is True, f"audio stream missing: {metadata}")
        require(metadata.get("audio_codec") == "aac", f"wrong audio codec: {metadata}")
        require(metadata.get("audio_channels") == 1, f"wrong channel count: {metadata}")
        require(metadata.get("audio_sample_rate") == 48_000, f"wrong sample rate: {metadata}")

        encoded_name = parse.quote(created_filename, safe="")
        with client.open("GET", f"/videos/{encoded_name}/thumbnail") as response:
            thumbnail = response.read()
        require(thumbnail.startswith(b"\xff\xd8") and thumbnail.endswith(b"\xff\xd9"), "thumbnail is not JPEG")

        with client.open(
            "GET",
            f"/videos/{encoded_name}",
            headers={"Range": "bytes=0-1023"},
        ) as response:
            range_status = response.status
            range_body = response.read()
        require(range_status == 206 and len(range_body) == 1024, "HTTP Range failed")

        video_path = videos_dir / created_filename
        require(video_path.is_file(), f"recording is missing: {video_path}")
        probe = ffprobe(video_path)
        streams = probe.get("streams") or []
        video_stream = next((item for item in streams if item.get("codec_type") == "video"), {})
        audio_stream = next((item for item in streams if item.get("codec_type") == "audio"), {})
        require(video_stream.get("width") == 1920 and video_stream.get("height") == 1080, "ffprobe video mismatch")
        require(audio_stream.get("codec_name") == "aac", "ffprobe audio mismatch")

        report.update(
            {
                "created_video": created_filename,
                "quality": quality,
                "metadata": metadata,
                "thumbnail_bytes": len(thumbnail),
                "range_bytes": len(range_body),
                "ffprobe": probe,
                "passed": True,
            }
        )
        return report
    finally:
        if recording_started:
            try:
                stopped = client.json("POST", "/stop", timeout=15 * 60)
                created_filename = Path(stopped.get("file", "")).name or created_filename
            except Exception as exc:
                report["cleanup_stop_error"] = str(exc)

        if created_filename and created_filename not in before_names:
            try:
                encoded_name = parse.quote(created_filename, safe="")
                client.json("DELETE", f"/delete/{encoded_name}")
            except Exception as exc:
                report["cleanup_delete_error"] = str(exc)

        try:
            restore_settings(client, original_settings)
        except Exception as exc:
            report["cleanup_settings_error"] = str(exc)

        after_names = {entry["filename"] for entry in video_entries(client, refresh=True)}
        if after_names != before_names:
            report["cleanup_library_error"] = {
                "before": sorted(before_names),
                "after": sorted(after_names),
            }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="https://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("MEDICAM_API_TOKEN", ""))
    parser.add_argument("--duration", type=float, default=10.0)
    parser.add_argument(
        "--videos-dir",
        type=Path,
        default=ROOT / "videos",
        help="filesystem directory used by the backend under test",
    )
    args = parser.parse_args()

    token = args.token or local_token()
    if not token:
        raise SystemExit("API token is required")
    if args.duration < 5:
        raise SystemExit("duration must be at least 5 seconds")

    try:
        report = run(
            ApiClient(args.base_url, token),
            args.duration,
            args.videos_dir.resolve(),
        )
    except Exception as exc:
        print(json.dumps({"passed": False, "error": str(exc)}, ensure_ascii=False, indent=2))
        return 1
    cleanup_errors = [key for key in report if key.startswith("cleanup_")]
    if cleanup_errors:
        report["passed"] = False
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report.get("passed") else 1


if __name__ == "__main__":
    sys.exit(main())
