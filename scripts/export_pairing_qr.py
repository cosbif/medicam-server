#!/usr/bin/env python3
"""Render a physical Medicam pairing package from trusted helper output."""

from __future__ import annotations

import argparse
import base64
import html
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlparse


class PairingInfoError(ValueError):
    """Raised when pairing information is missing, malformed, or inconsistent."""


def _required_line(output: str, label: str) -> str:
    match = re.search(
        rf"^{re.escape(label)}:\s*(\S+)\s*$",
        output.replace("\r", ""),
        flags=re.MULTILINE,
    )
    if not match:
        raise PairingInfoError(f"missing {label.lower()}")
    return match.group(1)


def parse_pairing_info(output: str) -> tuple[str, str, str]:
    """Validate helper output and return device ID, secret, and QR payload."""

    device_id = _required_line(output, "Device ID")
    grouped_code = _required_line(output, "Pairing code")
    payload = _required_line(output, "QR payload")
    code = re.sub(r"[-\s]", "", grouped_code).upper()

    if not re.fullmatch(r"[A-Fa-f0-9]{8,64}", device_id):
        raise PairingInfoError("invalid device ID")
    if not re.fullmatch(r"[A-Z2-7]{26}", code):
        raise PairingInfoError("invalid pairing code")

    uri = urlparse(payload)
    query = parse_qs(uri.query, strict_parsing=True)
    if uri.scheme != "medicam" or uri.netloc != "pair" or uri.path:
        raise PairingInfoError("invalid QR payload URI")
    if query != {"device_id": [device_id], "code": [code]}:
        raise PairingInfoError("QR payload does not match pairing information")
    return device_id, code, payload


def grouped_code(code: str) -> str:
    return "-".join(code[index:index + 4] for index in range(0, len(code), 4))


def _atomic_image(image: object, path: Path) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as stream:
            image.save(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_text(path: Path, value: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _label_svg(device_id: str, code: str, png_data: bytes) -> str:
    groups = grouped_code(code).split("-")
    first_line = "-".join(groups[:3])
    second_line = "-".join(groups[3:])
    encoded_png = base64.b64encode(png_data).decode("ascii")
    safe_id = html.escape(device_id)
    safe_first = html.escape(first_line)
    safe_second = html.escape(second_line)
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"
     width="60mm" height="40mm" viewBox="0 0 600 400">
  <rect width="600" height="400" fill="white"/>
  <image x="18" y="18" width="364" height="364"
         xlink:href="data:image/png;base64,{encoded_png}"/>
  <g fill="#111111" font-family="Arial, Helvetica, sans-serif">
    <text x="400" y="72" font-size="34" font-weight="700">NOM</text>
    <text x="400" y="105" font-size="22" font-weight="700">MEDICAM</text>
    <text x="400" y="160" font-size="17">Serial number</text>
    <text x="400" y="188" font-size="25" font-weight="700">{safe_id}</text>
    <text x="400" y="245" font-size="17">Pairing code</text>
    <text x="400" y="274" font-size="18" font-family="monospace">{safe_first}</text>
    <text x="400" y="302" font-size="18" font-family="monospace">{safe_second}</text>
    <text x="400" y="355" font-size="15">Keep this label private</text>
  </g>
</svg>
'''


def render_pairing_package(
    output_dir: Path,
    device_id: str,
    code: str,
    payload: str,
) -> dict[str, Path]:
    """Create scan, print, and fallback artifacts with private permissions."""

    try:
        import qrcode
        from qrcode.image.pure import PyPNGImage
        from qrcode.image.svg import SvgPathImage
    except ImportError as error:
        raise PairingInfoError(
            "QR dependencies are missing; run tool/export_pairing_qr.sh"
        ) from error

    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    if output_dir.is_symlink() or not output_dir.is_dir():
        raise PairingInfoError("output directory must be a regular directory")
    os.chmod(output_dir, 0o700)

    prefix = f"medicam-{device_id.lower()}-pairing"
    paths = {
        "png": output_dir / f"{prefix}.png",
        "svg": output_dir / f"{prefix}.svg",
        "label": output_dir / f"{prefix}-label.svg",
        "text": output_dir / f"{prefix}.txt",
    }

    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_Q,
        box_size=20,
        border=4,
    )
    qr.add_data(payload)
    qr.make(fit=True)
    _atomic_image(qr.make_image(image_factory=PyPNGImage), paths["png"])
    _atomic_image(qr.make_image(image_factory=SvgPathImage), paths["svg"])
    _atomic_text(
        paths["label"],
        _label_svg(device_id, code, paths["png"].read_bytes()),
    )
    _atomic_text(
        paths["text"],
        (
            "NOM Medicam physical pairing credential\n"
            f"Device ID: {device_id}\n"
            f"Pairing code: {grouped_code(code)}\n"
            f"QR payload: {payload}\n"
            "Keep this file private. Anyone with this credential and physical "
            "access to the camera can pair a new phone.\n"
        ),
    )
    for path in paths.values():
        os.chmod(path, 0o600)
    return paths


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a private QR pairing package from camera helper output."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args()
    try:
        device_id, code, payload = parse_pairing_info(sys.stdin.read())
        paths = render_pairing_package(arguments.output_dir, device_id, code, payload)
    except (OSError, PairingInfoError, ValueError) as error:
        print(f"Pairing QR export failed: {error}", file=sys.stderr)
        return 1

    print(f"Pairing package created for camera {device_id}:")
    print(f"  QR for scanning: {paths['png']}")
    print(f"  Vector QR:       {paths['svg']}")
    print(f"  Printable label: {paths['label']}")
    print(f"  Backup code:     {paths['text']}")
    print("Camera Bluetooth pairing window: 10 minutes")
    print("Directory permissions: 0700; file permissions: 0600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
