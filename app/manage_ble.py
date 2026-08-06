#!/usr/bin/env python3
import time
import subprocess
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app import utils

BLE_SERVICE = "medicam-ble.service"
LEGACY_BLE_SERVICES = ("ble-provision.service",)


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
    elif should_run and status != "active":
        if systemctl("start"):
            print("[Auto] BLE provisioning required → start BLE")


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
        provisioned = utils.is_provisioned()
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
        if current_state != previous_state:
            print(
                "[Auto] BLE state: "
                f"provisioned={provisioned} "
                f"wifi_connected={connected} "
                f"recovery_active={recovery_active} "
                f"boot_window_active={boot_window_active} "
                f"service={status} required={should_ble_run}"
            )
            previous_state = current_state

        reconcile_ble_service(
            should_run=should_ble_run,
            status=status,
        )

        time.sleep(10)

if __name__ == "__main__":
    main()
