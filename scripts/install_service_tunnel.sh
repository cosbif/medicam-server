#!/usr/bin/env bash
set -euo pipefail

BUNDLE_DIR="${1:-}"
CONFIG_DIR="/etc/medicam/service-tunnel"
MAINTENANCE_USER="medicam-maintenance"
MAINTENANCE_HOME="/var/lib/medicam-maintenance"
TUNNEL_UNIT="medicam-service-tunnel.service"

fail() {
    printf 'Service tunnel installation failed: %s\n' "$1" >&2
    exit 1
}

[[ ${EUID:-$(id -u)} -eq 0 ]] || fail "run through sudo"
[[ -n "$BUNDLE_DIR" && -d "$BUNDLE_DIR" && ! -L "$BUNDLE_DIR" ]] || \
    fail "pass the private camera bundle directory"

for name in camera.json tunnel_id_ed25519 server_known_hosts operator_authorized_key.pub; do
    [[ -f "$BUNDLE_DIR/$name" && ! -L "$BUNDLE_DIR/$name" ]] || \
        fail "bundle file is missing or unsafe: $name"
done

mapfile -t SETTINGS < <(
    /usr/bin/python3 - "$BUNDLE_DIR/camera.json" <<'PY'
import json
import re
import sys

with open(sys.argv[1], encoding="utf-8") as source:
    payload = json.load(source)
expected = {"serial_number", "tunnel_host", "tunnel_ssh_port", "reverse_port"}
if set(payload) != expected:
    raise SystemExit("camera.json has unexpected fields")
serial = str(payload["serial_number"])
host = str(payload["tunnel_host"])
ssh_port = payload["tunnel_ssh_port"]
reverse_port = payload["reverse_port"]
if not re.fullmatch(r"[A-Z0-9][A-Z0-9._-]{4,63}", serial):
    raise SystemExit("invalid serial number")
if not re.fullmatch(r"[A-Za-z0-9.-]{1,253}", host):
    raise SystemExit("invalid tunnel host")
if not isinstance(ssh_port, int) or not 1 <= ssh_port <= 65535:
    raise SystemExit("invalid tunnel SSH port")
if not isinstance(reverse_port, int) or not 20000 <= reverse_port <= 60999:
    raise SystemExit("invalid reverse port")
print(serial)
print(host)
print(ssh_port)
print(reverse_port)
PY
)
[[ ${#SETTINGS[@]} -eq 4 ]] || fail "invalid camera.json"
SERIAL_NUMBER="${SETTINGS[0]}"
TUNNEL_HOST="${SETTINGS[1]}"
TUNNEL_SSH_PORT="${SETTINGS[2]}"
TUNNEL_REVERSE_PORT="${SETTINGS[3]}"

/usr/bin/ssh-keygen -y -f "$BUNDLE_DIR/tunnel_id_ed25519" >/dev/null 2>&1 || \
    fail "invalid tunnel private key"
/usr/bin/ssh-keygen -l -f "$BUNDLE_DIR/operator_authorized_key.pub" >/dev/null 2>&1 || \
    fail "invalid operator public key"
/usr/bin/ssh-keygen -l -f "$BUNDLE_DIR/server_known_hosts" >/dev/null 2>&1 || \
    fail "invalid pinned server host key"

if ! getent passwd radxa >/dev/null; then
    fail "the radxa service account is missing"
fi
if ! getent passwd "$MAINTENANCE_USER" >/dev/null; then
    /usr/sbin/useradd \
        --create-home \
        --home-dir "$MAINTENANCE_HOME" \
        --shell /bin/bash \
        "$MAINTENANCE_USER"
fi
# A locked shadow entry can make sshd reject a valid public key. "x" is not a
# usable password hash, and all password authentication is disabled in the
# Medicam sshd policy installed below.
/usr/sbin/usermod --password 'x' "$MAINTENANCE_USER"

install -d -m 0700 -o radxa -g radxa "$CONFIG_DIR"
install -m 0600 -o radxa -g radxa \
    "$BUNDLE_DIR/tunnel_id_ed25519" "$CONFIG_DIR/id_ed25519"
install -m 0640 -o radxa -g radxa \
    "$BUNDLE_DIR/server_known_hosts" "$CONFIG_DIR/known_hosts"

ENV_TEMP="$(mktemp "$CONFIG_DIR/.tunnel.env.XXXXXX")"
cleanup() {
    rm -f -- "$ENV_TEMP"
}
trap cleanup EXIT
{
    printf 'MEDICAM_TUNNEL_HOST=%s\n' "$TUNNEL_HOST"
    printf 'MEDICAM_TUNNEL_SSH_PORT=%s\n' "$TUNNEL_SSH_PORT"
    printf 'MEDICAM_TUNNEL_REVERSE_PORT=%s\n' "$TUNNEL_REVERSE_PORT"
    printf 'MEDICAM_TUNNEL_USER=medicam-tunnel\n'
    printf 'MEDICAM_CAMERA_SERIAL=%s\n' "$SERIAL_NUMBER"
} >"$ENV_TEMP"
chown root:root "$ENV_TEMP"
chmod 0600 "$ENV_TEMP"
mv -f -- "$ENV_TEMP" "$CONFIG_DIR/tunnel.env"
trap - EXIT

SSH_DIR="$MAINTENANCE_HOME/.ssh"
install -d -m 0700 -o "$MAINTENANCE_USER" -g "$MAINTENANCE_USER" "$SSH_DIR"
AUTHORIZED_TEMP="$(mktemp "$SSH_DIR/.authorized_keys.XXXXXX")"
cleanup_authorized() {
    rm -f -- "$AUTHORIZED_TEMP"
}
trap cleanup_authorized EXIT
{
    printf 'from="127.0.0.1",no-agent-forwarding,no-port-forwarding,no-X11-forwarding '
    cat "$BUNDLE_DIR/operator_authorized_key.pub"
} >"$AUTHORIZED_TEMP"
chown "$MAINTENANCE_USER:$MAINTENANCE_USER" "$AUTHORIZED_TEMP"
chmod 0600 "$AUTHORIZED_TEMP"
mv -f -- "$AUTHORIZED_TEMP" "$SSH_DIR/authorized_keys"
trap - EXIT

SUDOERS_FILE="/etc/sudoers.d/medicam-maintenance"
printf '%s ALL=(ALL:ALL) NOPASSWD: ALL\n' "$MAINTENANCE_USER" >"$SUDOERS_FILE"
chmod 0440 "$SUDOERS_FILE"
/usr/sbin/visudo -cf "$SUDOERS_FILE" >/dev/null || fail "invalid sudoers policy"

install -m 0755 scripts/medicam_service_tunnel.sh \
    /usr/local/sbin/medicam-service-tunnel
install -m 0644 deploy/systemd/medicam-service-tunnel.service \
    "/etc/systemd/system/$TUNNEL_UNIT"
install -d -m 0755 /etc/ssh/sshd_config.d
install -m 0644 deploy/ssh/medicam.conf \
    /etc/ssh/sshd_config.d/99-medicam.conf
/usr/sbin/sshd -t || fail "refusing invalid SSH configuration"

systemctl disable --now medicam-cloud-agent.service 2>/dev/null || true
if [[ -f /etc/medicam/medicam.env ]]; then
    ENV_CLEAN="$(mktemp /etc/medicam/.medicam.env.XXXXXX)"
    grep -Ev '^MEDICAM_(CLOUD|REMOTE_VIDEO)_' /etc/medicam/medicam.env >"$ENV_CLEAN" || true
    chown root:root "$ENV_CLEAN"
    chmod 0600 "$ENV_CLEAN"
    mv -f -- "$ENV_CLEAN" /etc/medicam/medicam.env
fi

systemctl daemon-reload
systemctl reload ssh.service
systemctl enable --now "$TUNNEL_UNIT"
systemctl restart medicam.service

TUNNEL_STARTED=0
for _ in $(seq 1 10); do
    if systemctl is-active --quiet "$TUNNEL_UNIT"; then
        TUNNEL_STARTED=1
        break
    fi
    sleep 1
done
if [[ "$TUNNEL_STARTED" -ne 1 ]]; then
    journalctl -u "$TUNNEL_UNIT" -n 30 --no-pager >&2 || true
    fail "tunnel service did not become active"
fi

printf 'Service tunnel installed for %s on reverse port %s.\n' \
    "$SERIAL_NUMBER" "$TUNNEL_REVERSE_PORT"
printf 'Do not close the current local SSH session until the operator verifies the tunnel.\n'
