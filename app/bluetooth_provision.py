#!/usr/bin/env python3
import json
import subprocess
import time
from pathlib import Path

# bluezero imports
from bluezero import peripheral

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CMD_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
RESP_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef2"

PROVISION_FILE = Path("/home/radxa/medicam-server/provision.json")


def find_bt_adapter_candidates():
    """Возвращаем список кандидатов адаптеров в возможных форматах для bluezero."""
    candidates = []
    # 1) /sys/class/bluetooth (обычно содержит 'hci0')
    try:
        import os
        sysdir = "/sys/class/bluetooth"
        if os.path.exists(sysdir):
            for entry in sorted(os.listdir(sysdir)):
                if entry:
                    candidates.append(entry)  # 'hci0'
                    candidates.append(f"/org/bluez/{entry}")  # '/org/bluez/hci0'
    except Exception:
        pass

    # 2) попытка через bluetoothctl list (выдаст строки 'Controller MAC NAME ...')
    try:
        out = subprocess.check_output(["bluetoothctl", "list"], text=True, stderr=subprocess.DEVNULL).strip()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 2:
                # parts[1] обычно MAC-адрес адаптера
                mac = parts[1]
                candidates.append(mac)
    except Exception:
        pass

    # 3) безопасная последняя опция
    candidates.extend(["hci0", "/org/bluez/hci0"])
    # уберём дубликаты, сохраним порядок
    seen = set()
    uniq = []
    for c in candidates:
        if c not in seen:
            uniq.append(c)
            seen.add(c)
    return uniq


class ProvisionService:
    def __init__(self):
        self.response_value = b'{}'
        self.periph = None
        self.resp_char = None

        print("Finding bluetooth adapter...")
        adapters = find_bt_adapter_candidates()
        print(f"Adapter candidates: {adapters}")

        last_exc = None
        for a in adapters:
            try:
                print(f"Trying adapter: {a}")
                # Попробуем создать peripheral с очередным кандидатом
                self.periph = peripheral.Peripheral(adapter_address=a, local_name="MedicamProvision", object_path="/ukBaz/medicam")
                print(f"Peripheral created with adapter: {a}")
                break
            except Exception as e:
                print(f"Adapter {a} failed: {e}")
                last_exc = e
                self.periph = None

        if self.periph is None:
            raise RuntimeError(f"No usable Bluetooth adapter found. Last error: {last_exc}")

        # add_service требует (uuid, primary) — используем позиционные аргументы
        # primary=True означает, что это primary GATT service
        self.periph.add_service(SERVICE_UUID, True)

        # characteristic для команд (write)
        # signature: add_characteristic(service_uuid, uuid, flags, read_callback=None, write_callback=None, notify=False)
        self.periph.add_characteristic(SERVICE_UUID, CMD_CHAR_UUID, ['write'], write_callback=self.on_command)

        # characteristic для ответа (read + notify)
        self.resp_char = self.periph.add_characteristic(SERVICE_UUID, RESP_CHAR_UUID, ['read', 'notify'],
                                                        read_callback=self.on_read_response)

    # read callback должна возвращать байты
    def on_read_response(self):
        return self.response_value

    # write callback принимает (value, options)
    def on_command(self, value, options):
        try:
            data = json.loads(bytes(value).decode())
            print(f"Got command: {data}")
            cmd = data.get("cmd")

            if cmd == "PING":
                response = {"status": "OK"}

            elif cmd == "SCAN_WIFI":
                response = {"networks": self.scan_wifi()}

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

        resp_json = json.dumps(response).encode()
        self.response_value = resp_json

        # Отправляем нотификацию (если поддерживается)
        try:
            if hasattr(self.resp_char, "send_notify"):
                self.resp_char.send_notify(resp_json)
            else:
                # fallback: если API другой, пробуем метод notify
                self.resp_char.notify(resp_json)
        except Exception as e:
            print(f"Notify failed: {e}")

    def scan_wifi(self):
        try:
            result = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi"],
                capture_output=True, text=True, check=True
            )
            networks = []
            for line in result.stdout.strip().split("\n"):
                if not line:
                    continue
                parts = line.split(":")
                if len(parts) >= 2:
                    try:
                        signal = int(parts[1])
                    except Exception:
                        signal = 0
                    networks.append({"ssid": parts[0], "signal": signal})
            return networks
        except Exception as e:
            print(f"scan_wifi error: {e}")
            return []

    def connect_wifi(self, ssid, password):
        try:
            subprocess.run(
                ["nmcli", "dev", "wifi", "connect", ssid, "password", password],
                check=True
            )
            return True
        except subprocess.CalledProcessError as e:
            print(f"connect_wifi error: {e}")
            return False

    def write_provisioned(self, ssid=None):
        try:
            data = {"provisioned": True, "ssid": ssid}
            PROVISION_FILE.write_text(json.dumps(data))
            print("Provisioning complete: provision.json updated")
        except Exception as e:
            print(f"write_provisioned error: {e}")

    def run(self):
        print("Starting BLE Provisioning Service...")
        # publish() — метод bluezero для поднятия объявления, используем self.periph
        self.periph.publish()
        # Бесконечный цикл, чтобы скрипт не завершился
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            print("Shutting down provision service")


if __name__ == "__main__":
    ProvisionService().run()
