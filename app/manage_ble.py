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


def service_status():
    try:
        return subprocess.check_output(
            ["systemctl", "is-active", BLE_SERVICE],
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return "inactive"
    except Exception as e:
        print(f"[Auto] Failed to read BLE service status: {e}")
        return "unknown"


def systemctl(action: str):
    try:
        proc = subprocess.run(
            ["sudo", "/bin/systemctl", action, BLE_SERVICE],
            text=True,
            capture_output=True,
            timeout=10,
        )
        if proc.returncode != 0:
            print(
                f"[Auto] systemctl {action} failed: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )
        return proc.returncode == 0
    except Exception as e:
        print(f"[Auto] systemctl {action} exception: {e}")
        return False

def main():
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

        if not should_ble_run and status == "active":
            if systemctl("stop"):
                print("[Auto] Provisioned Wi-Fi active → stop BLE")
        elif should_ble_run and status != "active":
            if systemctl("start"):
                print(
                    "[Auto] BLE provisioning required → start BLE "
                    f"(provisioned={provisioned}, wifi_connected={connected}, "
                    f"recovery={recovery_active}, boot_window={boot_window_active})"
                )

        time.sleep(10)

if __name__ == "__main__":
    main()
