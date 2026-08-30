#!/usr/bin/env bash
set -euo pipefail

readonly MAINTENANCE_USER="medicam-maint"
readonly MAINTENANCE_HOME="/home/$MAINTENANCE_USER"
readonly SUDOERS_FILE="/etc/sudoers.d/medicam-maint"

if [[ "$EUID" -ne 0 ]]; then
  echo "Run this installer through sudo." >&2
  exit 1
fi
if [[ $# -ne 1 ]]; then
  echo "Usage: $0 /path/to/maintenance-key.pub" >&2
  exit 2
fi

PUBLIC_KEY_FILE="$1"
if [[ ! -f "$PUBLIC_KEY_FILE" || -L "$PUBLIC_KEY_FILE" ]]; then
  echo "The public key must be a regular, non-symlink file." >&2
  exit 1
fi

mapfile -t KEY_LINES < "$PUBLIC_KEY_FILE"
if [[ ${#KEY_LINES[@]} -ne 1 ]] ||
   [[ ! "${KEY_LINES[0]}" =~ ^ssh-ed25519[[:space:]]+[A-Za-z0-9+/]+={0,3}([[:space:]].*)?$ ]]; then
  echo "Expected exactly one OpenSSH Ed25519 public key." >&2
  exit 1
fi
if ! ssh-keygen -l -f "$PUBLIC_KEY_FILE" >/dev/null 2>&1; then
  echo "ssh-keygen rejected the maintenance public key." >&2
  exit 1
fi

if ! id "$MAINTENANCE_USER" >/dev/null 2>&1; then
  useradd \
    --system \
    --create-home \
    --home-dir "$MAINTENANCE_HOME" \
    --shell /bin/bash \
    --user-group \
    "$MAINTENANCE_USER"
fi

# An impossible password keeps public-key SSH available without enabling
# password authentication or creating a reusable credential on the camera.
usermod --password 'x' --shell /bin/bash "$MAINTENANCE_USER"
MAINTENANCE_GROUP="$(id -gn "$MAINTENANCE_USER")"
install -d -m 0700 -o "$MAINTENANCE_USER" -g "$MAINTENANCE_GROUP" \
  "$MAINTENANCE_HOME/.ssh"

AUTHORIZED_KEYS_TEMP="$(mktemp)"
SUDOERS_TEMP="$(mktemp)"
cleanup() {
  rm -f "$AUTHORIZED_KEYS_TEMP" "$SUDOERS_TEMP"
}
trap cleanup EXIT

# `restrict` disables forwarding, agent/X11 forwarding, PTY allocation and
# user rc files. Non-interactive shell commands and sudo -n remain available.
printf 'restrict %s\n' "${KEY_LINES[0]}" > "$AUTHORIZED_KEYS_TEMP"
install -m 0600 -o "$MAINTENANCE_USER" -g "$MAINTENANCE_GROUP" \
  "$AUTHORIZED_KEYS_TEMP" "$MAINTENANCE_HOME/.ssh/authorized_keys"

printf '%s ALL=(root) NOPASSWD: ALL\n' "$MAINTENANCE_USER" > "$SUDOERS_TEMP"
chmod 0440 "$SUDOERS_TEMP"
visudo -cf "$SUDOERS_TEMP" >/dev/null
install -m 0440 -o root -g root "$SUDOERS_TEMP" "$SUDOERS_FILE"
visudo -cf "$SUDOERS_FILE" >/dev/null

echo "Installed key-only maintenance access for $MAINTENANCE_USER."
echo "Password login and SSH forwarding remain disabled."
