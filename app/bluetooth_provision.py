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
        addr_path = Path("/sys/class/bluetooth/hci0/address")
        if addr_path.exists():
            mac = addr_path.read_text().strip()
            if mac:
                return mac
    except Exception:
        pass

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

        mac = get_adapter_mac()
        if not mac:
            raise RuntimeError(
                "Bluetooth adapter MAC not found. BLE cannot start.\n"
                "Check: hciconfig / bluetoothctl / system logs"
            )

        print(f"[BLE] Using adapter MAC: {mac}")

        # создаём Peripheral
        self.periph = peripheral.Peripheral(
            adapter_address=mac,
            local_name="MedicamProvision"
        )

        print("[BLE] Peripheral created via bluezero")

        # Добавляем сервис и характеристики; сохраняем объекты характер.
        SRV_ID = 1
        self.periph.add_service(SRV_ID, SERVICE_UUID, True)

        # Command characteristic (write)
        # Возвращаем объект, если bluezero возвращает его (но add_characteristic
        # у bluezero иногда возвращает None — поэтому мы не завязываемся полностью на возврат)
        try:
            self.cmd_char = self.periph.add_characteristic(
                srv_id=SRV_ID,
                chr_id=1,
                uuid=CMD_CHAR_UUID,
                value=[],
                notifying=False,
                flags=["write"],
                read_callback=None,
                write_callback=self.on_command
            )
        except TypeError:
            # старые/разные сигнатуры библиотеки
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
            self.cmd_char = None

        # Response characteristic (read + notify) — сохраним ссылку, если possible
        try:
            resp = self.periph.add_characteristic(
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
            self.resp_char = resp
        except TypeError:
            # альтернативная сигнатура
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
            self.resp_char = None

        self.srv_id = SRV_ID
        self.resp_chr_id = 2
        self.response_value = b'{}'

    # === BLE methods ===
    def on_read_response(self):
        # Bluezero expects list of ints as value
        return list(self.response_value)

    def _send_response(self, response_dict):
        """
        Устанавливаем value и пытаемся отправить notify через объект resp_char.
        Если resp_char неизвестен, просто логируем и позволяем клиенту делать Read.
        """
        self.response_value = json.dumps(response_dict).encode()
        value_list = list(self.response_value)

        # Если у нас есть прямой объект характеристики — используем его
        if self.resp_char is not None:
            try:
                # set_value — стандартный метод у bluezero Characteristic
                if hasattr(self.resp_char, "set_value"):
                    self.resp_char.set_value(value_list)
                else:
                    # если нет set_value, пробуем установить через periph API
                    try:
                        self.periph.set_characteristic_value(self.srv_id, self.resp_chr_id, value_list)
                    except Exception:
                        pass

                # Попытка уведомления (несколько возможных имен методов)
                if getattr(self.resp_char, "notifying", False):
                    # уже notifying
                    try:
                        if hasattr(self.resp_char, "send_notify"):
                            self.resp_char.send_notify()
                        elif hasattr(self.resp_char, "notify"):
                            self.resp_char.notify()
                    except Exception as e:
                        print("[WARN] notify methods failed:", e)
                else:
                    # Попытка вызвать notify в любом случае (если поддерживается)
                    try:
                        if hasattr(self.resp_char, "send_notify"):
                            self.resp_char.send_notify()
                        elif hasattr(self.resp_char, "notify"):
                            self.resp_char.notify()
                    except Exception as e:
                        # если уведомление невозможно — просто логируем
                        print("[WARN] notify failed (notifying unsupported):", e)

                return
            except Exception as e:
                print("[WARN] notify failed (resp_char block):", e)

        # fallback: пробуем пройти по списку характеристик и установить значение,
        # но не полагаться на атрибуты которых может не быть
        try:
            for ch in getattr(self.periph, "characteristics", []) or []:
                try:
                    # пробуем установить значение, если есть метод set_value
                    if hasattr(ch, "set_value"):
                        ch.set_value(value_list)
                        # если у неё notifying — пробуем notify
                        if getattr(ch, "notifying", False):
                            if hasattr(ch, "send_notify"):
                                ch.send_notify()
                            elif hasattr(ch, "notify"):
                                ch.notify()
                        # не ломаем цикл — мы установили значение
                        break
                except Exception:
                    continue
        except Exception as e:
            print("[WARN] notify failed (fallback):", e)

    def on_command(self, value, options):
        self._cmd_buffer = bytearray()
        try:
            # value может приходить кусками — добавляем в буфер
            if isinstance(value, (bytes, bytearray)):
                self._cmd_buffer.extend(value)
            else:
                self._cmd_buffer.extend(bytearray(value))

            # JSON по BLE приходит как текст UTF-8.
            # Проверим, закончился ли JSON (последняя } )
            if self._cmd_buffer and self._cmd_buffer[-1] != ord('}'):
                # не полный JSON — ждем
                return

            # получили полный JSON
            raw = bytes(self._cmd_buffer)
            self._cmd_buffer.clear()

            data = json.loads(raw.decode())
            cmd = data.get("cmd")
            print("[BLE] Command:", cmd)

            if cmd == "PING":
                response = {"status": "OK"}

            elif cmd == "SCAN_WIFI":
                nets = self.scan_wifi()
                response = {"networks": nets}

            elif cmd == "CONNECT_WIFI":
                ssid = data.get("ssid")
                password = data.get("password")
                ok = self.connect_wifi(ssid, password)
                response = {"status": "connected"} if ok else {"status": "failed"}

            else:
                response = {"error": "unknown_command"}

        except Exception as e:
            response = {"error": str(e)}

        self._send_response(response)

    # === Wi-Fi ===
    def scan_wifi(self):
        """
        Возвращаем топ N сетей с безопасной длиной SSID.
        Это снижает размер JSON и уменьшает вероятность проблем с BLE MTU.
        """
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
                parts = line.split(":", 1)
                if len(parts) != 2:
                    continue
                ssid, signal_str = parts
                ssid = ssid.strip()
                if not ssid or ssid in seen:
                    continue
                seen.add(ssid)
                try:
                    signal = int(signal_str) if signal_str.isdigit() else 0
                except Exception:
                    signal = 0
                # отбрасываем слабые
                if signal >= 30:
                    # обрезаем ssid до 64 символов для безопасности
                    safe_ssid = ssid[:64]
                    networks.append({"ssid": safe_ssid, "signal": signal})
            # сортируем и ограничиваем количество результатов
            networks = sorted(networks, key=lambda x: -x["signal"])
            return networks[:10]  # максимум 10 сетей
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

    def _get_first_ipv4(self):
        try:
            out = subprocess.check_output(["ip", "-4", "addr", "show", "scope", "global"], text=True)
            import re
            m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
            return m.group(1) if m else ""
        except Exception:
            return ""

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
