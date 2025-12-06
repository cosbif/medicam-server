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

# Попробуем импортировать bluezero; если его нет — скрипт всё равно аккуратно упадёт
try:
    from bluezero import peripheral
except Exception as e:
    print(f"[ERR] bluezero import failed: {e}")
    peripheral = None

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CMD_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
RESP_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef2"

PROVISION_FILE = utils._provision_path()

def is_wifi_connected() -> bool:
    """Проверяем, есть ли активное подключение к Wi-Fi."""
    try:
        status = subprocess.check_output(["nmcli", "-t", "-f", "STATE", "g"], text=True).strip()
        return "connected" in status.lower()
    except Exception as e:
        print(f"[WARN] Wi-Fi check failed: {e}")
        return False

def get_adapter_name_or_mac():
    """Пытаемся определить рабочий адаптер. Возвращаем MAC (если удалось) или 'hci0'."""
    # Попробуем получить MAC через hciconfig
    try:
        out = subprocess.check_output(["hciconfig"], text=True)
        # пример строки: "hci0:   Type: Primary  Bus: USB"
        # затем строка "BD Address: XX:XX:XX:XX:XX:XX"
        lines = out.splitlines()
        mac = None
        for i, ln in enumerate(lines):
            if ln.startswith("hci"):
                # ищем BD Address в последующих строках
                for j in range(i, min(i+4, len(lines))):
                    if "BD Address" in lines[j]:
                        parts = lines[j].split()
                        for p in parts:
                            if ":" in p and len(p.split(":")) == 6:
                                mac = p.strip()
                                return mac
        # fallback
        return "hci0"
    except Exception:
        return "hci0"

class ProvisionService:
    def __init__(self):
        self.response_value = b'{}'
        self.periph = None
        self.resp_char = None

        if peripheral is None:
            raise RuntimeError("bluezero.peripheral is not available")

        adapter_candidate = get_adapter_name_or_mac()
        print(f"[BLE] Adapter candidate: {adapter_candidate}")

        # Попытки инициализировать peripheral с разными аргументами (в зависимости от API/версии)
        tried = []
        created = False
        for attempt in range(2):
            try:
                if ":" in adapter_candidate:
                    # возможно ожидается adapter_addr (MAC)
                    self.periph = peripheral.Peripheral(adapter_addr=adapter_candidate,
                                                        local_name="MedicamProvision")
                else:
                    # возможно ожидается adapter_name
                    self.periph = peripheral.Peripheral(adapter_name=adapter_candidate,
                                                        local_name="MedicamProvision")
                print(f"[BLE] Peripheral created with {adapter_candidate}")
                created = True
                break
            except TypeError as te:
                # аргумент не подходит — попробуем альтернативный ключ
                tried.append(str(te))
                # попробуем другой формат: если была mac, попробуем hci0, и наоборот
                adapter_candidate = "hci0" if adapter_candidate != "hci0" else get_adapter_name_or_mac()
            except Exception as e:
                tried.append(str(e))
                adapter_candidate = "hci0"

        if not created or not self.periph:
            raise RuntimeError(f"No valid BLE adapter found. Attempts: {tried}")

        # Добавляем сервис и характеристики
        self.periph.add_service(SERVICE_UUID, True)
        self.periph.add_characteristic(SERVICE_UUID, CMD_CHAR_UUID, ['write'], write_callback=self.on_command)
        self.resp_char = self.periph.add_characteristic(
            SERVICE_UUID, RESP_CHAR_UUID, ['read', 'notify'], read_callback=self.on_read_response
        )

    def on_read_response(self):
        return self.response_value

    def _send_response(self, response_dict: dict):
        try:
            self.response_value = json.dumps(response_dict).encode()
            # попытка уведомления (notify)
            try:
                # некоторые версии bluezero используют notify() или send_notify()
                if hasattr(self.resp_char, "send_notify"):
                    self.resp_char.send_notify(self.response_value)
                elif hasattr(self.resp_char, "notify"):
                    self.resp_char.notify(self.response_value)
                else:
                    # просто обновим значение — клиент может читать
                    pass
            except Exception as e:
                print(f"[WARN] notify failed: {e}")
        except Exception as e:
            print(f"[ERR] _send_response: {e}")

    def on_command(self, value, options):
        try:
            # value может быть байт-последовательностью или list(int)
            if isinstance(value, (bytes, bytearray)):
                raw = bytes(value)
            else:
                # возможно list of ints
                raw = bytes(bytearray(value))
            data = json.loads(raw.decode())
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
                    # после подключения получим ip
                    ip = ""
                    try:
                        ip_out = subprocess.check_output(["ip", "-4", "addr", "show", "scope", "global"], text=True)
                        import re
                        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", ip_out)
                        ip = m.group(1) if m else ""
                    except Exception:
                        ip = ""
                    utils.set_provisioned(True, {"ssid": ssid, "ip": ip})
                    response = {"status": "connected", "ip": ip}
                else:
                    response = {"status": "failed"}

            else:
                response = {"error": "unknown_command"}

        except Exception as e:
            response = {"error": str(e)}

        self._send_response(response)

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
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue
                ssid, signal_str = parts
                ssid = ssid.strip()
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
            if password:
                subprocess.run(["nmcli", "dev", "wifi", "connect", ssid, "password", password], check=True, timeout=60)
            else:
                subprocess.run(["nmcli", "dev", "wifi", "connect", ssid], check=True, timeout=60)
            return True
        except subprocess.CalledProcessError as e:
            print(f"[ERR] connect_wifi: {e}")
            return False
        except Exception as e:
            print(f"[ERR] connect_wifi unexpected: {e}")
            return False

    def run(self):
        print("[BLE] Starting provisioning service...")
        try:
            self.periph.publish()
        except Exception as e:
            print(f"[ERR] publish failed: {e}")
            raise

        try:
            while True:
                # если Wi-Fi стал подключён — прекращаем провижн
                if is_wifi_connected():
                    print("[BLE] Detected Wi-Fi connected -> stopping BLE provisioning")
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
            print(f"[WARN] BLE stop failed: {e}")


if __name__ == "__main__":
    if is_wifi_connected():
        print("[BLE] Wi-Fi connected — BLE provisioning disabled.")
    else:
        try:
            ProvisionService().run()
        except Exception as e:
            print(f"[FATAL] BLE provisioning failed to start: {e}")
