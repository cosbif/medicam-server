#!/usr/bin/env bash
set -euo pipefail

DROP_IN_DIR="/etc/systemd/system/medicam.service.d"
OTA_STATE_DIR="/var/lib/medicam-ota"
OTA_CONFIG_DIR="/etc/medicam"
OTA_HELPER="/usr/local/sbin/medicam-ota-activate"

sudo install -d -m 0755 "$DROP_IN_DIR"
sudo tee "$DROP_IN_DIR/runtime.conf" >/dev/null <<'UNIT'
[Service]
RuntimeDirectory=medicam
RuntimeDirectoryMode=0750
# Preserve the RAM-backed PCM file across a service restart so the recovery
# manifest can finalize an interrupted recording. A full reboot still clears
# /run, in which case the raw video is recovered without audio.
RuntimeDirectoryPreserve=restart
UNIT

# Install the independent activator, then let its signed-release `harden`
# action materialize and install every privileged asset from the verified
# commit rather than this mutable checkout.
sudo install -d -m 0770 -o root -g radxa "$OTA_STATE_DIR"
sudo install -d -m 0755 "$OTA_CONFIG_DIR" /usr/local/sbin
sudo install -m 0755 scripts/medicam_ota_activate.py "$OTA_HELPER"
sudo install -m 0644 deploy/ota_allowed_signers \
    "$OTA_CONFIG_DIR/ota_allowed_signers"
sudo install -m 0644 deploy/image-version "$OTA_CONFIG_DIR/image-version"
sudo "$OTA_HELPER" harden

echo "Installed Medicam runtime directory: /run/medicam"
echo "Installed signed OTA activator: $OTA_HELPER"
