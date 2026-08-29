#!/usr/bin/env bash
set -euo pipefail

fail() {
    printf 'Medicam service tunnel: %s\n' "$1" >&2
    exit 1
}

TUNNEL_HOST="${MEDICAM_TUNNEL_HOST:-}"
TUNNEL_SSH_PORT="${MEDICAM_TUNNEL_SSH_PORT:-}"
TUNNEL_REVERSE_PORT="${MEDICAM_TUNNEL_REVERSE_PORT:-}"
TUNNEL_USER="${MEDICAM_TUNNEL_USER:-medicam-tunnel}"
IDENTITY_FILE="/etc/medicam/service-tunnel/id_ed25519"
KNOWN_HOSTS_FILE="/etc/medicam/service-tunnel/known_hosts"

[[ "$TUNNEL_HOST" =~ ^[A-Za-z0-9.-]{1,253}$ ]] || fail "invalid host"
[[ "$TUNNEL_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "invalid user"
[[ "$TUNNEL_SSH_PORT" =~ ^[0-9]{1,5}$ ]] || fail "invalid SSH port"
[[ "$TUNNEL_REVERSE_PORT" =~ ^[0-9]{1,5}$ ]] || fail "invalid reverse port"
(( TUNNEL_SSH_PORT >= 1 && TUNNEL_SSH_PORT <= 65535 )) || fail "SSH port out of range"
(( TUNNEL_REVERSE_PORT >= 20000 && TUNNEL_REVERSE_PORT <= 60999 )) || \
    fail "reverse port out of range"
[[ -r "$IDENTITY_FILE" && -r "$KNOWN_HOSTS_FILE" ]] || fail "credentials missing"

exec /usr/bin/ssh \
    -F /dev/null \
    -NT \
    -i "$IDENTITY_FILE" \
    -o BatchMode=yes \
    -o ConnectTimeout=10 \
    -o ExitOnForwardFailure=yes \
    -o IdentitiesOnly=yes \
    -o PasswordAuthentication=no \
    -o ServerAliveCountMax=3 \
    -o ServerAliveInterval=30 \
    -o StrictHostKeyChecking=yes \
    -o "UserKnownHostsFile=$KNOWN_HOSTS_FILE" \
    -p "$TUNNEL_SSH_PORT" \
    -R "127.0.0.1:$TUNNEL_REVERSE_PORT:127.0.0.1:22" \
    "$TUNNEL_USER@$TUNNEL_HOST"
