#!/usr/bin/env bash
set -euo pipefail

SOURCE_TOKEN="${1:-/home/radxa/medicam-cloud-bootstrap.pending}"
CLOUD_URL="${2:-https://api.medicam-cloud.ru}"
CONFIG_DIR="/etc/medicam"
TOKEN_FILE="$CONFIG_DIR/cloud-bootstrap-token"
ENV_FILE="$CONFIG_DIR/medicam.env"
STATE_FILE="/var/lib/medicam/cloud-state.json"

fail() {
    printf 'Cloud enrollment setup failed: %s\n' "$1" >&2
    exit 1
}

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    fail "run this installer through sudo"
fi
if [[ ! "$CLOUD_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?$ ]]; then
    fail "cloud URL must be an HTTPS origin without credentials, path, or query"
fi
if [[ -L "$SOURCE_TOKEN" || ! -f "$SOURCE_TOKEN" ]]; then
    fail "bootstrap token must be a regular file"
fi
SOURCE_MODE="$(stat -c '%a' "$SOURCE_TOKEN")"
if (( (8#$SOURCE_MODE & 8#077) != 0 )); then
    fail "bootstrap token must not be readable by group or other users"
fi
if [[ -e "$STATE_FILE" ]]; then
    fail "cloud state already exists; refusing to enroll the device again"
fi
if [[ -e "$ENV_FILE" || -e "$TOKEN_FILE" ]]; then
    fail "cloud configuration already exists; inspect it before replacing anything"
fi
if ! getent passwd radxa >/dev/null || ! getent group radxa >/dev/null; then
    fail "the radxa service account is missing"
fi

TOKEN="$(<"$SOURCE_TOKEN")"
if [[ ! "$TOKEN" =~ ^[A-Za-z0-9_-]{43,128}$ ]]; then
    fail "bootstrap token has an invalid format"
fi

install -d -m 0755 -o root -g root "$CONFIG_DIR"
TOKEN_TEMP="$(mktemp "$CONFIG_DIR/.cloud-bootstrap-token.XXXXXX")"
ENV_TEMP="$(mktemp "$CONFIG_DIR/.medicam.env.XXXXXX")"
cleanup() {
    rm -f -- "$TOKEN_TEMP" "$ENV_TEMP"
}
trap cleanup EXIT

printf '%s\n' "$TOKEN" >"$TOKEN_TEMP"
chown radxa:radxa "$TOKEN_TEMP"
chmod 0600 "$TOKEN_TEMP"

{
    printf 'MEDICAM_CLOUD_URL=%s\n' "$CLOUD_URL"
    printf 'MEDICAM_REMOTE_VIDEO_ENABLED=1\n'
} >"$ENV_TEMP"
chown root:root "$ENV_TEMP"
chmod 0600 "$ENV_TEMP"

mv -f -- "$TOKEN_TEMP" "$TOKEN_FILE"
mv -f -- "$ENV_TEMP" "$ENV_FILE"
trap - EXIT

# Enroll before restarting the camera backend. This prevents the heartbeat
# agent and remote-video worker from racing to consume the one-time token.
systemctl enable --now medicam-cloud-agent.service

ENROLLED=0
for _ in $(seq 1 30); do
    if [[ -s "$STATE_FILE" ]] && python3 - "$STATE_FILE" "$CLOUD_URL" <<'PY'
import json
import sys

path, expected_url = sys.argv[1:]
try:
    data = json.load(open(path, encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
valid = (
    data.get("server_url") == expected_url
    and isinstance(data.get("device_id"), str)
    and 8 <= len(data["device_id"]) <= 32
    and isinstance(data.get("device_token"), str)
    and len(data["device_token"]) >= 32
)
raise SystemExit(0 if valid else 1)
PY
    then
        ENROLLED=1
        break
    fi
    if [[ "$(systemctl is-failed medicam-cloud-agent.service || true)" == "failed" ]]; then
        break
    fi
    sleep 1
done

if [[ "$ENROLLED" -ne 1 ]]; then
    printf 'Cloud agent did not enroll within 30 seconds. Recent log:\n' >&2
    journalctl -u medicam-cloud-agent.service -n 20 --no-pager >&2 || true
    systemctl stop medicam-cloud-agent.service || true
    rm -f -- "$TOKEN_FILE" "$ENV_FILE"
    exit 1
fi

# The bootstrap is one-time and the permanent credential now lives in the
# mode-0600 state file. Remove both copies before enabling remote video.
rm -f -- "$TOKEN_FILE" "$SOURCE_TOKEN"
systemctl restart medicam.service

printf 'Cloud enrollment completed; one-time bootstrap removed.\n'
printf 'Cloud agent: %s\n' "$(systemctl is-active medicam-cloud-agent.service)"
printf 'Camera backend: %s\n' "$(systemctl is-active medicam.service)"
