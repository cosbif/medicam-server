#!/usr/bin/env python3
import json
import subprocess
import time
from pathlib import Path
import os
import sys
import threading
import traceback

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

# ---------------------------
# Helper: adapter MAC
# ---------------------------
def get_adapter_mac():
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

    return None

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

# ---------------------------
# Provision service class
# ---------------------------
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

        self.periph = peripheral.Peripheral(
            adapter_address=mac,
            local_name="MedicamProvision"
        )

        print("[BLE] Peripheral created via bluezero")

        SRV_ID = 1
        self.periph.add_service(SRV_ID, SERVICE_UUID, True)

        # Command characteristic (write)
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
            # fallback for different bluezero versions
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

        # Response characteristic (read + notify)
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
        # lock to protect response_value between threads
        self._resp_lock = threading.Lock()
        # buffer for fragmented incoming writes (persistent across calls)
        self._cmd_buffer = bytearray()

    # ---------------------------
    # Read callback for RESP
    # ---------------------------
    def on_read_response(self):
        with self._resp_lock:
            return list(self.response_value)

    # ---------------------------
    # Internal: set response and try notify (non-blocking)
    # ---------------------------
    def _set_response(self, response_dict):
        """
        Устанавливает response_value. Пытаться отправить notify,
        но не блокировать основной поток — ошибки логируем.
        """
        try:
            payload = json.dumps(response_dict).encode()
        except Exception:
            payload = str(response_dict).encode()

        value_list = list(payload)
        with self._resp_lock:
            self.response_value = payload

        # Попытка установить value и послать notify — всё в try/except
        try:
            if self.resp_char is not None:
                # try to use direct API
                try:
                    if hasattr(self.resp_char, "set_value"):
                        self.resp_char.set_value(value_list)
                except Exception:
                    # fallback: try peripheral helper
                    try:
                        if hasattr(self.periph, "set_characteristic_value"):
                            self.periph.set_characteristic_value(self.srv_id, self.resp_chr_id, value_list)
                    except Exception:
                        pass

                # try to notify if supported
                try:
                    if getattr(self.resp_char, "notifying", False):
                        if hasattr(self.resp_char, "send_notify"):
                            self.resp_char.send_notify()
                        elif hasattr(self.resp_char, "notify"):
                            self.resp_char.notify()
                except Exception as e:
                    print("[WARN] notify attempt failed:", e)
            else:
                # fallback: iterate over periph.characteristics and set first matching
                for ch in getattr(self.periph, "characteristics", []) or []:
                    try:
                        if getattr(ch, "uuid", None) == RESP_CHAR_UUID and hasattr(ch, "set_value"):
                            ch.set_value(value_list)
                            # try notify
                            try:
                                if getattr(ch, "notifying", False):
                                    if hasattr(ch, "send_notify"):
                                        ch.send_notify()
                                    elif hasattr(ch, "notify"):
                                        ch.notify()
                            except Exception:
                                pass
                            break
                    except Exception:
                        continue
        except Exception as e:
            print("[WARN] _set_response notify block error:", e)

    # ---------------------------
    # Long-running workers
    # ---------------------------
    def _worker_scan_wifi(self):
        try:
            networks = self.scan_wifi()
            self._set_response({"networks": networks})
            print("[BLE] SCAN_WIFI finished, networks count:", len(networks))
        except Exception as e:
            print("[ERR] worker_scan_wifi:", e, traceback.format_exc())
            self._set_response({"error": str(e)})

    def _worker_connect_wifi(self, ssid, password):
        try:
            ok = self.connect_wifi(ssid, password)
            ip = ""
            if ok:
                # get first ip
                try:
                    out = subprocess.check_output(["ip", "-4", "addr", "show", "scope", "global"], text=True)
                    import re
                    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
                    ip = m.group(1) if m else ""
                except Exception:
                    ip = ""
                utils.set_provisioned(True, {"ssid": ssid, "ip": ip})
                self._set_response({"status": "connected", "ip": ip})
            else:
                self._set_response({"status": "failed"})
        except Exception as e:
            print("[ERR] worker_connect_wifi:", e, traceback.format_exc())
            self._set_response({"error": str(e)})

    # ---------------------------
    # Command handler (fast return)
    # ---------------------------
    def on_command(self, value, options):
        """
        Принимаем куски данных (value может быть bytes или list(int)).
        Накапливаем в self._cmd_buffer и пытаемся распарсить JSON.
        Если JSON некорректен из-за неполноты — ждём следующего вызова.
        Если JSON распарсен успешно — обрабатываем команду.
        """
        try:
            # Получаем байты из value
            if isinstance(value, (bytes, bytearray)):
                chunk = bytes(value)
            else:
                chunk = bytes(bytearray(value))

            # Добавляем фрагмент в общий буфер
            if chunk:
                self._cmd_buffer.extend(chunk)

            # Попытка распарсить содержимое буфера как JSON
            try:
                text = self._cmd_buffer.decode()
            except UnicodeDecodeError:
                # Если ещё не полный UTF-8 фрагмент — ждём продолжения
                return

            try:
                data = json.loads(text)
            except json.JSONDecodeError as jde:
                # Неполный JSON — ждём следующих фрагментов
                return

            # Если дошли сюда — JSON успешно распарсен
            # Очищаем буфер (готовы принимать следующий JSON)
            self._cmd_buffer.clear()

            cmd = data.get("cmd")
            print("[BLE] Command:", cmd)

            if cmd == "PING":
                self._set_response({"status": "OK"})
                return

            if cmd == "SCAN_WIFI":
                t = threading.Thread(target=self._worker_scan_wifi, daemon=True)
                t.start()
                self._set_response({"status": "started_scan"})
                return

            if cmd == "CONNECT_WIFI":
                ssid = data.get("ssid")
                password = data.get("password")
                t = threading.Thread(target=self._worker_connect_wifi, args=(ssid, password), daemon=True)
                t.start()
                self._set_response({"status": "connecting"})
                return

            # unknown
            self._set_response({"error": "unknown_command"})
        except Exception as e:
            print("[ERR] on_command top-level:", e, traceback.format_exc())
            self._set_response({"error": str(e)})

    # ---------------------------
    # Wi-Fi helpers (unchanged, non-blocking now moved to worker)
    # ---------------------------
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
                if signal >= 30:
                    safe_ssid = ssid[:64]
                    networks.append({"ssid": safe_ssid, "signal": signal})
            networks = sorted(networks, key=lambda x: -x["signal"])
            return networks[:10]
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

    # ---------------------------
    # Main loop
    # ---------------------------
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
