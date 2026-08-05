#!/usr/bin/env python3
"""Exercise the real recording routes and verify every resulting video."""

import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from urllib import error, request
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FPS = 30.0
MIN_AVG_FPS = 29.5
MIN_FRAME_DELIVERY = 0.995


def _local_api_token():
    try:
        from app import utils

        return utils.get_api_token()
    except Exception:
        return ""


def _api(base_url, token, method, path, timeout=15, form=None):
    data = urlencode(form).encode("utf-8") if form else None
    headers = {"X-Medicam-Token": token}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = request.Request(
        f"{base_url.rstrip('/')}{path}",
        method=method,
        headers=headers,
        data=data,
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path}: HTTP {exc.code}: {body}") from exc


def _rate(value):
    numerator, denominator = str(value).split("/", maxsplit=1)
    return float(numerator) / float(denominator) if float(denominator) else 0.0


def _probe(path):
    probe_timeout = max(120.0, path.stat().st_size / (2 * 1024 * 1024) + 120.0)
    result = subprocess.run(
        [
            "ffprobe",
            "-v", "error",
            "-count_frames",
            "-select_streams", "v:0",
            "-show_entries",
            "stream=nb_read_frames,avg_frame_rate,width,height:format=duration",
            "-of", "json",
            str(path),
        ],
        text=True,
        capture_output=True,
        timeout=probe_timeout,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "ffprobe failed")
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("ffprobe found no video stream")
    stream = streams[0]
    return {
        "duration_seconds": float((data.get("format") or {}).get("duration") or 0),
        "frames": int(stream.get("nb_read_frames") or 0),
        "avg_fps": _rate(stream.get("avg_frame_rate", "0/1")),
        "resolution": f"{stream.get('width')}x{stream.get('height')}",
    }


def _run_one(base_url, token, duration, poll_interval):
    before = _api(base_url, token, "GET", "/recording/status")
    if before.get("recording"):
        raise RuntimeError(
            f"camera is not idle: state={before.get('state')} file={before.get('file')}"
        )

    settings = _api(
        base_url,
        token,
        "POST",
        "/settings",
        form={"resolution": "FHD", "fps": "30"},
    )
    if settings.get("resolution") != "FHD" or str(settings.get("fps")) != "30":
        raise RuntimeError(f"camera rejected FullHD 30 fps settings: {settings}")

    started = _api(base_url, token, "POST", "/start")
    if started.get("status") != "recording_started":
        raise RuntimeError(f"recording did not start: {started}")
    if started.get("resolution") != "1920x1080" or str(started.get("fps")) != "30":
        raise RuntimeError(f"unexpected recording mode: {started}")

    started_monotonic = time.monotonic()
    latest = None
    try:
        while True:
            elapsed = time.monotonic() - started_monotonic
            if elapsed >= duration:
                break
            time.sleep(min(poll_interval, duration - elapsed))
            latest = _api(base_url, token, "GET", "/recording/status")
            if latest.get("state") != "recording" or not latest.get("capture_active"):
                raise RuntimeError(f"capture became unhealthy after {elapsed:.1f}s: {latest}")
    finally:
        stopped = _api(base_url, token, "POST", "/stop", timeout=3 * 60 * 60)

    if stopped.get("status") != "recording_stopped" or stopped.get("returncode") != 0:
        raise RuntimeError(f"recording did not finalize: {stopped}")

    relative_path = stopped.get("file") or ""
    video_path = (ROOT / relative_path).resolve()
    videos_root = (ROOT / "videos").resolve()
    if videos_root not in video_path.parents or video_path.suffix.lower() != ".mp4":
        raise RuntimeError(f"server returned an unsafe output path: {relative_path}")
    if not video_path.is_file():
        raise RuntimeError(f"output file is missing: {video_path}")

    server_quality = stopped.get("quality") or {}
    if (
        server_quality.get("valid")
        and server_quality.get("frame_count") is not None
    ):
        # /stop already performs a full ffprobe -count_frames validation. Reuse
        # that canonical result instead of decoding a long MJPEG file twice.
        probe = {
            "duration_seconds": float(server_quality["duration_seconds"]),
            "frames": int(server_quality["frame_count"]),
            "avg_fps": float(server_quality["avg_fps"]),
            "resolution": server_quality.get("resolution", ""),
        }
    else:
        probe = _probe(video_path)
    expected_frames = round(duration * FPS)
    missing_frames = max(0, expected_frames - probe["frames"])
    delivery = min(1.0, probe["frames"] / expected_frames)
    passed = (
        probe["resolution"] == "1920x1080"
        and probe["avg_fps"] >= MIN_AVG_FPS
        and delivery >= MIN_FRAME_DELIVERY
        and server_quality.get("healthy") is True
    )
    return {
        "requested_duration_seconds": duration,
        "file": relative_path,
        "file_size_bytes": video_path.stat().st_size,
        "expected_frames": expected_frames,
        "actual_frames": probe["frames"],
        "missing_frames": missing_frames,
        "frame_delivery_ratio": round(delivery, 6),
        "duration_seconds": round(probe["duration_seconds"], 3),
        "avg_fps": round(probe["avg_fps"], 3),
        "resolution": probe["resolution"],
        "server_quality": server_quality,
        "passed": passed,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("MEDICAM_API_TOKEN", ""))
    parser.add_argument(
        "--durations",
        default="60,600,1800",
        help="comma-separated recording durations in seconds",
    )
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--report", default="recording-long-run-report.json")
    args = parser.parse_args()

    token = args.token or _local_api_token()
    if not token:
        raise SystemExit("API token is required (--token or MEDICAM_API_TOKEN)")
    durations = [int(value) for value in args.durations.split(",") if value.strip()]
    if not durations or any(value <= 0 for value in durations):
        raise SystemExit("durations must contain positive integers")

    reports = []
    overall_passed = True
    for duration in durations:
        print(f"[recording-test] starting {duration}s FullHD 30 fps run", flush=True)
        try:
            report = _run_one(
                args.base_url,
                token,
                duration,
                max(0.5, args.poll_interval),
            )
        except Exception as exc:
            report = {
                "requested_duration_seconds": duration,
                "passed": False,
                "error": str(exc),
            }
        reports.append(report)
        overall_passed = overall_passed and report["passed"]
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
        if not report["passed"]:
            break

    output = {
        "passed": overall_passed and len(reports) == len(durations),
        "fps_target": FPS,
        "minimum_avg_fps": MIN_AVG_FPS,
        "minimum_frame_delivery_ratio": MIN_FRAME_DELIVERY,
        "runs": reports,
    }
    Path(args.report).write_text(
        json.dumps(output, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[recording-test] report: {args.report}", flush=True)
    return 0 if output["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
