#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${MEDICAM_SERVER_DIR:-/home/radxa/medicam-server}"
PYTHON_BIN="${MEDICAM_PYTHON:-$APP_DIR/medicam-venv/bin/python3}"

if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "Python venv executable is missing: $PYTHON_BIN" >&2
  exit 1
fi

sudo tee /etc/systemd/system/medicam-ble.service >/dev/null <<UNIT
[Unit]
Description=Medicam BLE Wi-Fi Provisioning
After=bluetooth.service NetworkManager.service
Wants=bluetooth.service NetworkManager.service

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON_BIN $APP_DIR/app/bluetooth_provision.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo tee /etc/systemd/system/medicam-ble-manager.service >/dev/null <<UNIT
[Unit]
Description=Medicam BLE Provisioning Manager
After=NetworkManager.service bluetooth.service
Wants=NetworkManager.service bluetooth.service

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
Environment=PYTHONUNBUFFERED=1
ExecStart=$PYTHON_BIN $APP_DIR/app/manage_ble.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl disable --now ble-provision.service 2>/dev/null || true
sudo systemctl enable medicam-ble.service medicam-ble-manager.service
sudo systemctl restart medicam-ble-manager.service
sudo systemctl restart medicam-ble.service || true

systemctl --no-pager --full status medicam-ble-manager.service || true
systemctl --no-pager --full status medicam-ble.service || true
