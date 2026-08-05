#!/usr/bin/env bash
set -euo pipefail

ACTIVATOR="/usr/local/sbin/medicam-ota-activate"
if [[ ! -x "$ACTIVATOR" ]]; then
  echo "Install the signed server runtime first: scripts/install_server_runtime.sh" >&2
  exit 1
fi

# Re-run the signed hardening path. It installs BLE code and its interpreter
# under root-owned /opt/medicam before touching systemd.
sudo "$ACTIVATOR" harden
sudo systemctl disable --now ble-provision.service 2>/dev/null || true
sudo systemctl restart medicam-ble-manager.service
sudo systemctl restart medicam-ble.service || true

systemctl --no-pager --full status medicam-ble-manager.service || true
systemctl --no-pager --full status medicam-ble.service || true
