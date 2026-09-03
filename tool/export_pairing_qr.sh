#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CAMERA_HOST="${1:-nom.local}"
OUTPUT_DIR="${2:-$PROJECT_ROOT/pairing-output/$(date '+%Y%m%d-%H%M%S')}"
VENV_DIR="${MEDICAM_QR_VENV:-$PROJECT_ROOT/.tools/pairing-qr-venv}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

case "$CAMERA_HOST" in
  -*|*[[:space:]]*)
    printf 'Invalid camera SSH host: %s\n' "$CAMERA_HOST" >&2
    exit 2
    ;;
esac

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

if ! "$VENV_DIR/bin/python" -c 'import png, qrcode' >/dev/null 2>&1; then
  "$VENV_DIR/bin/python" -m pip install \
    --disable-pip-version-check \
    --requirement "$PROJECT_ROOT/tool/requirements-pairing-qr.txt"
fi

mkdir -p "$OUTPUT_DIR"
chmod 700 "$OUTPUT_DIR"

# The maintenance key is intentionally restricted and cannot allocate a PTY.
# The approved helper uses non-interactive sudo, so a plain SSH command keeps
# QR export compatible with the hardened production access policy.
ssh -T "$CAMERA_HOST" \
  'sudo -n /usr/local/sbin/medicam-ota-activate pairing-info' \
  | tr -d '\r' \
  | "$VENV_DIR/bin/python" "$PROJECT_ROOT/scripts/export_pairing_qr.py" \
      --output-dir "$OUTPUT_DIR"
