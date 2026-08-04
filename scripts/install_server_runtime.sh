#!/usr/bin/env bash
set -euo pipefail

DROP_IN_DIR="/etc/systemd/system/medicam.service.d"

sudo install -d -m 0755 "$DROP_IN_DIR"
sudo tee "$DROP_IN_DIR/runtime.conf" >/dev/null <<'UNIT'
[Service]
RuntimeDirectory=medicam
RuntimeDirectoryMode=0750
UNIT

sudo systemctl daemon-reload
sudo systemctl enable medicam.service

echo "Installed Medicam runtime directory: /run/medicam"
