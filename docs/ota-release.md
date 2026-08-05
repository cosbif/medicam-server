# Medicam signed OTA releases

Production devices do not update from `origin/main`. They only discover stable
tags matching `medicam-vMAJOR.MINOR.PATCH` and accept them after SSH signature
verification against the root-owned allowlist on the device.

## Release procedure

1. Finish code review and tests on `main`, then push the commit to GitHub.
2. Confirm the worktree is clean.
3. Create and publish the signed tag:

   ```bash
   ./scripts/create_ota_release.sh 1.2.3
   ```

4. Open the app update screen. It must display the signed tag and verified
   commit hash before enabling installation.
5. After rollout, verify `/version` and `/update/status` on at least one device.

Published version numbers are immutable and must never be reused. If an
uninstalled release must be revoked, delete its remote tag; the next channel
check prunes that local tag from devices. A release that reached any device
must instead be superseded by a higher signed version because the root-owned
anti-downgrade counter is advanced only after a successful healthcheck.

The dedicated private key defaults to
`~/.config/medicam/ota_signing_key`. It must never be committed or copied to a
camera. Back it up in an encrypted offline secret store. Losing the key prevents
new updates; leaking it requires a signed key-rotation release and immediate
revocation of the old key.

## Device trust and rollback

`/usr/local/sbin/medicam-ota-activate` is root-owned. Before activation it
re-verifies the tag with `/etc/medicam/ota_allowed_signers`, checks that the tag
resolves to the requested commit, rejects signed downgrades, and verifies the
root-owned previous commit.

The activator restarts the backend from an independent transient systemd unit.
`/ping` must return the expected commit within 60 seconds. Otherwise the helper
restores the previous checkout, Python dependencies, systemd units, and image
metadata. The reason and journal tail are persisted in
`/var/lib/medicam-ota/status.json` and shown in the app.

The service user has no general passwordless sudo. It can only request a
strictly validated signed activation and start/restart the BLE provisioning
unit.
