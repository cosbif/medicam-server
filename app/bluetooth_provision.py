#!/usr/bin/env python3
import json
import subprocess
import time
from pathlib import Path
import os
import sys

# подключаем project root, чтобы импортировать app.utils
project_root = Path(__file__).resolve().parents[1]
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app import utils

try:
    from bluezero import peripheral
except Exception as e:
    print(f"[ERR] bluezero import failed: {e}")
    peripheral = None

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CMD_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
RESP_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef2"

PROVISION_FILE = utils._provision_path()

TEST_MODE = True  # временный флаг для отладки BLE


# ===============================
# === FIX: корректные MAC-адрес ==
# ===============================
def get_adapter_mac():
    """
    Возвращает MAC адрес адаптера hci0.
    Bluezero требует MAC, а не имя интерфейса!
    """
    try:
        # Самый надёжный способ (есть всегда)
        addr_path = Path("/sys/class/bluetooth/hci0/address")
        if addr_path.exists():
            mac = addr_path.read_text().strip()
            if mac:
                return mac
    except Exception:
        pass

    # Fallback через hciconfig
    try:
        out = subprocess.check_output(["hciconfig"], text=True)
        for line in out.splitlines():
            if "BD Address" in line:
                parts = line.split()
                for p in parts:
                    if ":" in p and len(p.split(":")) == 6:
                        return p
    except Exception:
        pass

    return None  # важно вернуть None, если не нашли


def is_wifi_connected() -> bool:
    if TEST_MODE:
        return False
    try:
        status = subprocess.check_output(
            ["nmcli", "-t", "-f", "STATE", "g"],
            text=True
        ).strip()
        return "connected" in status.lower()
    except Exception as e:
        print(f"[WARN] Wi-Fi check failed: {e}")
        return False


class ProvisionService:
    def __init__(self):
        if peripheral is None:
            raise RuntimeError("bluezero.peripheral is not available")

        # ===========================
        # === FIX: получаем MAC ====
        # ===========================
        mac = get_adapter_mac()
        if not mac:
            raise RuntimeError(
                "Bluetooth adapter MAC not found. BLE cannot start.\n"
                "Check: hciconfig / bluetoothctl / system logs"
            )

        print(f"[BLE] Using adapter MAC: {mac}")

        # ===========================
        # === FIX: создаём Peripheral
        # ===========================
        self.periph = peripheral.Peripheral(
            adapter_address=mac,
            local_name="MedicamProvision"
        )

        print("[BLE] Peripheral created via bluezero")

        # === Сервис ===
        SRV_ID = 1
        self.periph.add_service(SRV_ID, SERVICE_UUID, True)

        # === Write команда ===
        self.periph.add_characteristic(
            srv_id=SRV_ID,
            chr_id=1,
            uuid=CMD_CHAR_UUID,
            value=[],
            notifying=False,
            flags=["write"],
            read_callback=None,
            write_callback=self.on_command
        )

        # === Ответная характеристика ===
        self.periph.add_characteristic(
            srv_id=SRV_ID,
            chr_id=2,
            uuid=RESP_CHAR_UUID,
            value=[],
            notifying=False,
            flags=["read", "notify"],
            read_callback=self.on_read_response,
            write_callback=None,
            notify_callback=None
        )

        self.srv_id = SRV_ID
        self.resp_chr_id = 2
        self.response_value = b'{}'


    # === BLE methods ===
    def on_read_response(self):
        return list(self.response_value)

    def _send_response(self, response_dict):
        self.response_value = json.dumps(response_dict).encode()
        value_list = list(self.response_value)

        try:
            for ch in self.periph.characteristics:
                if ch.uuid == RESP_CHAR_UUID:
                    ch.set_value(value_list)
                    if ch.notifying:
                        ch.send_notify()
                    break
        except Exception as e:
            print("[WARN] notify failed:", e)

    def on_command(self, value, options):
        try:
            raw = bytes(value)
            data = json.loads(raw.decode())
            cmd = data.get("cmd")
            print("[BLE] Command:", cmd)

            if cmd == "PING":
                response = {"status": "OK"}

            elif cmd == "SCAN_WIFI":
                response = {"networks": self.scan_wifi()}

            elif cmd == "CONNECT_WIFI":
                ssid = data.get("ssid")
                password = data.get("password")
                ok = self.connect_wifi(ssid, password)

                if ok:
                    ip = utils.get_ip_address()
                    utils.set_provisioned(True, {"ssid": ssid, "ip": ip})
                    response = {"status": "connected", "ip": ip}
                else:
                    response = {"status": "failed"}

            else:
                response = {"error": "unknown_command"}

        except Exception as e:
            response = {"error": str(e)}

        self._send_response(response)


    # === Wi-Fi ===
    def scan_wifi(self):
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi"],
                capture_output=True,
                text=True,
                check=True
            )
            seen = set()
            networks = []
            for line in result.stdout.splitlines():
                if not line:
                    continue
                ssid, signal_str = line.split(":", 1)
                ssid = ssid.strip()
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                signal = int(signal_str) if signal_str.isdigit() else 0
                if signal >= 50:
                    networks.append({"ssid": ssid, "signal": signal})
            return sorted(networks, key=lambda x: -x["signal"])
        except Exception as e:
            print(f"[ERR] scan_wifi: {e}")
            return []

    def connect_wifi(self, ssid, password):
        try:
            if password:
                subprocess.run(["nmcli", "dev", "wifi", "connect", ssid,
                                "password", password], check=True, timeout=60)
            else:
                subprocess.run(["nmcli", "dev", "wifi", "connect", ssid],
                               check=True, timeout=60)
            return True
        except Exception as e:
            print(f"[ERR] connect_wifi: {e}")
            return False


    # === Main Loop ===
    def run(self):
        print("[BLE] Starting provisioning service...")

        try:
            self.periph.publish()
        except Exception as e:
            print(f"[ERR] publish failed: {e}")
            raise

        try:
            while True:
                if is_wifi_connected():
                    print("[BLE] Wi-Fi connected -> stopping BLE")
                    break
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()

    def stop(self):
        try:
            print("[BLE] Stopping service...")
            self.periph.unpublish()
        except Exception as e:
            print("[WARN] BLE stop failed:", e)


if __name__ == "__main__":
    print("[BLE] TEST MODE ACTIVE — ignoring Wi-Fi status")
    try:
        ProvisionService().run()
    except Exception as e:
        print(f"[FATAL] BLE provisioning failed to start: {e}")
