#!/usr/bin/env python3
import json
import subprocess
import time
from pathlib import Path
import os
from bluezero import peripheral

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CMD_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
RESP_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef2"

PROVISION_FILE = Path("/home/radxa/medicam-server/provision.json")


def is_wifi_connected() -> bool:
    """Проверяем, есть ли активное подключение к Wi-Fi."""
    try:
        result = subprocess.check_output(["nmcli", "-t", "-f", "WIFI", "g"], text=True).strip()
        if result.lower() != "enabled":
            return False
        status = subprocess.check_output(["nmcli", "-t", "-f", "STATE", "g"], text=True).strip()
        return "connected" in status.lower()
    except Exception as e:
        print(f"[WARN] Wi-Fi check failed: {e}")
        return False


def find_bt_adapter_candidates():
    """Ищем рабочие BLE адаптеры."""
    candidates = []
    try:
        sysdir = "/sys/class/bluetooth"
        if os.path.exists(sysdir):
            for entry in sorted(os.listdir(sysdir)):
                candidates.append(entry)
                candidates.append(f"/org/bluez/{entry}")
    except Exception:
        pass
    candidates.extend(["hci0", "/org/bluez/hci0"])
    return list(dict.fromkeys(candidates))  # remove duplicates


class ProvisionService:
    def __init__(self):
        self.response_value = b'{}'
        self.periph = None
        self.resp_char = None

        adapters = find_bt_adapter_candidates()
        print(f"Found adapters: {adapters}")

        for a in adapters:
            try:
                self.periph = peripheral.Peripheral(
                    adapter_address=a,
                    local_name="MedicamProvision",
                    object_path="/ukBaz/medicam"
                )
                print(f"[OK] Using adapter {a}")
                break
            except Exception as e:
                print(f"[ERR] Adapter {a} failed: {e}")
                self.periph = None

        if not self.periph:
            raise RuntimeError("No valid BLE adapter found")

        self.periph.add_service(SERVICE_UUID, True)
        self.periph.add_characteristic(SERVICE_UUID, CMD_CHAR_UUID, ['write'], write_callback=self.on_command)
        self.resp_char = self.periph.add_characteristic(
            SERVICE_UUID, RESP_CHAR_UUID, ['read', 'notify'], read_callback=self.on_read_response
        )

    def on_read_response(self):
        return self.response_value

    def on_command(self, value, options):
        try:
            data = json.loads(bytes(value).decode())
            cmd = data.get("cmd")
            print(f"[BLE] Command received: {cmd}")

            if cmd == "PING":
                response = {"status": "OK"}

            elif cmd == "SCAN_WIFI":
                nets = self.scan_wifi()
                response = {"networks": nets}

            elif cmd == "CONNECT_WIFI":
                ssid = data.get("ssid")
                password = data.get("password")
                success = self.connect_wifi(ssid, password)
                if success:
                    self.write_provisioned(ssid)
                    response = {"status": "connected"}
                else:
                    response = {"status": "failed"}

            else:
                response = {"error": "unknown_command"}

        except Exception as e:
            response = {"error": str(e)}

        self.response_value = json.dumps(response).encode()
        try:
            self.resp_char.send_notify(self.response_value)
        except Exception as e:
            print(f"[WARN] Notify failed: {e}")

    def scan_wifi(self):
        """Возвращаем только сильные сигналы (>50)."""
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi"],
                capture_output=True, text=True, check=True
            )
            seen = set()
            networks = []
            for line in result.stdout.strip().splitlines():
                if not line:
                    continue
                ssid, signal_str = (line.split(":", 1) + ["0"])[:2]
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                try:
                    signal = int(signal_str)
                except ValueError:
                    signal = 0
                if signal >= 50:
                    networks.append({"ssid": ssid, "signal": signal})
            return sorted(networks, key=lambda x: -x["signal"])
        except Exception as e:
            print(f"[ERR] scan_wifi: {e}")
            return []

    def connect_wifi(self, ssid, password):
        try:
            subprocess.run(["nmcli", "dev", "wifi", "connect", ssid, "password", password], check=True)
            return True
        except subprocess.CalledProcessError as e:
            print(f"[ERR] connect_wifi: {e}")
            return False

    def write_provisioned(self, ssid=None):
        try:
            data = {"provisioned": True, "ssid": ssid}
            PROVISION_FILE.write_text(json.dumps(data))
        except Exception as e:
            print(f"[ERR] write_provisioned: {e}")

    def run(self):
        print("[BLE] Starting provisioning service...")
        self.periph.publish()
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            self.stop()

    def stop(self):
        try:
            print("[BLE] Stopping service...")
            self.periph.unpublish()
        except Exception as e:
            print(f"[WARN] BLE stop failed: {e}")


if __name__ == "__main__":
    if is_wifi_connected():
        print("[BLE] Wi-Fi connected — BLE provisioning disabled.")
    else:
        ProvisionService().run()
