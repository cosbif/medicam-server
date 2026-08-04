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
    peripheral = None
    peripheral_import_error = e
else:
    peripheral_import_error = None

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CMD_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
RESP_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef2"
PROTOCOL_VERSION = 2
MAX_COMMAND_BYTES = 8192

PROVISION_FILE = utils._provision_path()

TEST_MODE = os.getenv("MEDICAM_BLE_TEST_MODE", "").lower() in {
    "1",
    "true",
    "yes",
}

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
    return utils.is_wifi_connected()


def get_wifi_ssid() -> str:
    if TEST_MODE:
        return ""
    return utils.get_wifi_ssid()

# ---------------------------
# Provision service class
# ---------------------------
class ProvisionService:
    def __init__(self):
        if peripheral is None:
            raise RuntimeError(
                f"bluezero.peripheral is not available: {peripheral_import_error}"
            )

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
                flags=["write-without-response", "write"],
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
                flags=["write-without-response", "write"],
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
    def _set_response(self, response_dict, request_id=None):
        """
        Устанавливает response_value. Пытаться отправить notify,
        но не блокировать основной поток — ошибки логируем.
        """
        if request_id is not None:
            response_dict = {
                **response_dict,
                "request_id": request_id,
            }

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

    def _set_response_async(self, response_dict, request_id=None):
        threading.Thread(
            target=self._set_response,
            args=(response_dict,),
            kwargs={"request_id": request_id},
            daemon=True,
        ).start()

    def _dispatch_command(self, data):
        """
        Handle commands outside BlueZ's WriteValue callback.

        Calling _set_response() synchronously from WriteValue can make BlueZ
        wait on a nested D-Bus characteristic update before it sends the ATT
        Write Response. iOS then times out the write. Returning from the write
        callback immediately keeps the GATT transaction healthy.
        """
        threading.Thread(
            target=self._handle_command,
            args=(data,),
            daemon=True,
        ).start()

    def _status_payload(self):
        return {
            "status": "ok",
            "protocol": PROTOCOL_VERSION,
            "device": "Medicam",
            "provisioned": utils.is_provisioned(),
            "wifi_connected": is_wifi_connected(),
            "ssid": get_wifi_ssid(),
            "ip": "" if TEST_MODE else utils.get_primary_ipv4(),
        }

    # ---------------------------
    # Long-running workers
    # ---------------------------
    def _worker_scan_wifi(self, request_id=None):
        try:
            networks = self.scan_wifi()
            self._set_response({"networks": networks}, request_id=request_id)
            print("[BLE] SCAN_WIFI finished, networks count:", len(networks))
        except Exception as e:
            print("[ERR] worker_scan_wifi:", e, traceback.format_exc())
            self._set_response({"error": str(e)}, request_id=request_id)

    def _worker_connect_wifi(self, ssid, password, request_id=None):
        try:
            result = self.connect_wifi(ssid, password)
            ok = bool(result.get("ok"))
            ip = result.get("ip", "")
            if ok:
                utils.set_provisioned(True, {"ssid": ssid, "ip": ip})
                api_token = utils.get_api_token()
                self._set_response(
                    {
                        "status": "connected",
                        "ip": ip,
                        "api_token": api_token,
                    },
                    request_id=request_id,
                )
            else:
                self._set_response(
                    {
                        "status": "failed",
                        "stderr": result.get("stderr", ""),
                    },
                    request_id=request_id,
                )
        except Exception as e:
            print("[ERR] worker_connect_wifi:", e, traceback.format_exc())
            self._set_response({"error": str(e)}, request_id=request_id)

    # ---------------------------
    # Command handler (fast return)
    # ---------------------------
    def on_command(self, value, options=None):
        """Accept newline-framed JSON commands, including fragmented BLE writes."""
        try:
            if isinstance(value, (bytes, bytearray)):
                chunk = bytes(value)
            else:
                chunk = bytes(bytearray(value))

            if chunk:
                self._cmd_buffer.extend(chunk)

            if len(self._cmd_buffer) > MAX_COMMAND_BYTES:
                self._cmd_buffer.clear()
                self._set_response_async({"error": "command_too_large"})
                return

            handled = False
            while b"\n" in self._cmd_buffer:
                raw_message, _, rest = self._cmd_buffer.partition(b"\n")
                self._cmd_buffer = bytearray(rest)
                raw_message = raw_message.strip()
                if not raw_message:
                    continue
                data = json.loads(raw_message.decode("utf-8"))
                self._dispatch_command(data)
                handled = True

            if handled:
                return

            # Backward compatibility: older clients sent a single JSON write
            # without newline framing. Keep supporting it for manual testing.
            try:
                text = self._cmd_buffer.decode("utf-8")
                data = json.loads(text)
            except (UnicodeDecodeError, json.JSONDecodeError):
                return

            self._cmd_buffer.clear()
            self._dispatch_command(data)
        except Exception as e:
            print("[ERR] on_command top-level:", e, traceback.format_exc())
            self._set_response_async({"error": str(e)})

    def _handle_command(self, data):
        if not isinstance(data, dict):
            self._set_response({"error": "invalid_command"})
            return

        cmd = data.get("cmd")
        request_id = data.get("request_id")
        print("[BLE] Command:", cmd)

        if cmd in {"PING", "STATUS"}:
            self._set_response(self._status_payload(), request_id=request_id)
            return

        if cmd == "SCAN_WIFI":
            self._worker_scan_wifi(request_id=request_id)
            return

        if cmd == "CONNECT_WIFI":
            ssid = data.get("ssid")
            password = data.get("password")
            if not isinstance(ssid, str) or not ssid:
                self._set_response(
                    {"error": "ssid_required"},
                    request_id=request_id,
                )
                return

            self._worker_connect_wifi(ssid, password, request_id=request_id)
            return

        self._set_response(
            {"error": "unknown_command"},
            request_id=request_id,
        )

    # ---------------------------
    # Wi-Fi helpers (unchanged, non-blocking now moved to worker)
    # ---------------------------
    def scan_wifi(self):
        try:
            result = subprocess.run(
                [
                    "nmcli",
                    "-t",
                    "--escape",
                    "yes",
                    "-f",
                    "SSID,SIGNAL,SECURITY",
                    "dev",
                    "wifi",
                    "list",
                    "--rescan",
                    "yes",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="ignore",
                check=True
            )
            by_ssid = {}
            for line in result.stdout.splitlines():
                if not line:
                    continue
                parts = utils.split_nmcli_escaped(line)
                if len(parts) < 2:
                    continue
                ssid, signal_str = parts[0], parts[1]
                security = parts[2] if len(parts) > 2 else ""
                ssid = ssid.strip()
                if not ssid:
                    continue
                try:
                    signal = int(signal_str) if signal_str.isdigit() else 0
                except Exception:
                    signal = 0
                if signal >= 30:
                    current = by_ssid.get(ssid)
                    if current is None or signal > current["signal"]:
                        by_ssid[ssid] = {
                            "ssid": ssid[:64],
                            "signal": signal,
                            "secured": bool(security.strip()),
                        }
            networks = list(by_ssid.values())
            networks = sorted(networks, key=lambda x: -x["signal"])
            return networks[:12]
        except Exception as e:
            print(f"[ERR] scan_wifi: {e}")
            return []

    def connect_wifi(self, ssid, password):
        result = utils.connect_wifi_nmcli(ssid, password, timeout=60)
        if not result.get("ok"):
            print(f"[ERR] connect_wifi: {result.get('stderr')}")
        return result

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
                if utils.is_provisioned() and is_wifi_connected():
                    print("[BLE] Device provisioned and Wi-Fi connected -> stopping BLE")
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
    if TEST_MODE:
        print("[BLE] TEST MODE ACTIVE — ignoring Wi-Fi status")
    try:
        ProvisionService().run()
    except Exception as e:
        print(f"[FATAL] BLE provisioning failed to start: {e}")
