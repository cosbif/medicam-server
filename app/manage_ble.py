#!/usr/bin/env python3
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app import utils

BLE_SERVICE = "medicam-ble.service"
LEGACY_BLE_SERVICES = ("ble-provision.service",)
BLE_MANAGER_STATE_FILE = Path(
    os.environ.get(
        "MEDICAM_BLE_MANAGER_STATE_FILE",
        "/var/lib/medicam/ble-manager-state.json",
    )
)


def should_run_ble(
    provisioned: bool,
    connected: bool,
    recovery_active: bool,
    boot_window_active: bool,
) -> bool:
    return (
        (not provisioned)
        or (not connected)
        or recovery_active
        or boot_window_active
    )


def service_status(unit: str = BLE_SERVICE):
    try:
        return subprocess.check_output(
            ["systemctl", "is-active", unit],
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return "inactive"
    except Exception as e:
        print(f"[Auto] Failed to read BLE service status: {e}")
        return "unknown"


def systemctl(action: str, unit: str = BLE_SERVICE):
    try:
        proc = subprocess.run(
            ["/bin/systemctl", action, unit],
            text=True,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode != 0:
            print(
                f"[Auto] systemctl {action} {unit} failed: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.returncode == 0
    except Exception as e:
        print(f"[Auto] systemctl {action} exception: {e}")
        return False


def reconcile_ble_service(*, should_run: bool, status: str):
    """Apply one manager iteration; recovery refresh uses systemd.path."""
    if not should_run and status == "active":
        if systemctl("stop"):
            print("[Auto] Provisioned Wi-Fi active → stop BLE")
            return "stopped"
        return "stop_failed"
    elif should_run and status != "active":
        if systemctl("start"):
            print("[Auto] BLE provisioning required → start BLE")
            return "started"
        return "start_failed"
    return "unchanged"


def write_manager_state(state: dict) -> None:
    """Publish non-secret manager state without following attacker links."""
    path = BLE_MANAGER_STATE_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory = os.open(path.parent, directory_flags)
    temporary_name = f".{path.name}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
    descriptor = None
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory,
        )
        payload = json.dumps(state, sort_keys=True).encode("utf-8") + b"\n"
        os.write(descriptor, payload)
        os.fsync(descriptor)
        os.fchmod(descriptor, 0o644)
        os.close(descriptor)
        descriptor = None
        os.replace(
            temporary_name,
            path.name,
            src_dir_fd=directory,
            dst_dir_fd=directory,
        )
        os.fsync(directory)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            os.unlink(temporary_name, dir_fd=directory)
        except FileNotFoundError:
            pass
        os.close(directory)


def disable_legacy_ble_services():
    """Remove obsolete units that otherwise publish duplicate GATT services."""
    for unit in LEGACY_BLE_SERVICES:
        status = service_status(unit)
        enabled = False
        try:
            enabled = subprocess.run(
                ["/bin/systemctl", "is-enabled", "--quiet", unit],
                timeout=5,
            ).returncode == 0
        except Exception:
            pass

        if status == "active" and systemctl("stop", unit):
            print(f"[Auto] Stopped legacy BLE service: {unit}")
        if enabled and systemctl("disable", unit):
            print(f"[Auto] Disabled legacy BLE service: {unit}")


def main():
    disable_legacy_ble_services()
    previous_state = None
    while True:
        provisioned = utils.is_ble_provisioned()
        connected = utils.is_wifi_connected()
        recovery_active = utils.is_ble_recovery_active()
        boot_window_active = utils.is_boot_pairing_window_active()
        status = service_status()

        should_ble_run = should_run_ble(
            provisioned,
            connected,
            recovery_active,
            boot_window_active,
        )

        current_state = (
            provisioned,
            connected,
            recovery_active,
            boot_window_active,
            status,
            should_ble_run,
        )
        action = reconcile_ble_service(
            should_run=should_ble_run,
            status=status,
        )

        if current_state != previous_state:
            print(
                "[Auto] BLE state: "
                f"provisioned={provisioned} "
                f"wifi_connected={connected} "
                f"recovery_active={recovery_active} "
                f"boot_window_active={boot_window_active} "
                f"service={status} required={should_ble_run}"
            )
            try:
                write_manager_state(
                    {
                        "observed_at_unix": time.time(),
                        "provisioned": provisioned,
                        "wifi_connected": connected,
                        "recovery_active": recovery_active,
                        "boot_window_active": boot_window_active,
                        "service": status,
                        "required": should_ble_run,
                        "action": action,
                    }
                )
            except Exception as error:
                print(f"[Auto] Failed to publish BLE manager state: {error}")
            previous_state = current_state

        time.sleep(10)

if __name__ == "__main__":
    main()
