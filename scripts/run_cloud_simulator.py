#!/usr/bin/env python3
"""Run the production cloud agent as a simulated Radxa device on macOS."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from app.cloud_agent import main  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cloud-url", required=True)
    parser.add_argument("--device-id", required=True)
    parser.add_argument(
        "--bootstrap-token-file",
        help="path to a mode-0600 file containing the one-time token",
    )
    parser.add_argument(
        "--state-file",
        default=str(REPO / ".medicam-cloud-simulator.json"),
    )
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


if __name__ == "__main__":
    arguments = parse_args()
    os.environ["MEDICAM_CLOUD_URL"] = arguments.cloud_url
    os.environ["MEDICAM_CLOUD_DEVICE_ID"] = arguments.device_id
    if arguments.bootstrap_token_file:
        os.environ["MEDICAM_CLOUD_BOOTSTRAP_TOKEN_FILE"] = (
            arguments.bootstrap_token_file
        )
    os.environ["MEDICAM_CLOUD_STATE_FILE"] = arguments.state_file
    raise SystemExit(main(["--once"] if arguments.once else []))
