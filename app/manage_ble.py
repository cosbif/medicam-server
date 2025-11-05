#!/usr/bin/env python3
import time
import subprocess
import os

BLE_SERVICE = "medicam-ble.service"

def wifi_connected():
    try:
        out = subprocess.check_output(["nmcli", "-t", "-f", "STATE", "g"], text=True).strip()
        return "connected" in out
    except Exception:
        return False

def main():
    while True:
        connected = wifi_connected()
        try:
            status = subprocess.check_output(["systemctl", "is-active", BLE_SERVICE], text=True).strip()
        except subprocess.CalledProcessError:
            status = "inactive"

        if connected and status == "active":
            os.system(f"sudo systemctl stop {BLE_SERVICE}")
            print("[Auto] Wi-Fi active → stop BLE")
        elif not connected and status != "active":
            os.system(f"sudo systemctl start {BLE_SERVICE}")
            print("[Auto] No Wi-Fi → start BLE")

        time.sleep(10)

if __name__ == "__main__":
    main()
