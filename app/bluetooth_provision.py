#!/usr/bin/env python3
"""
Bluetooth SPP server for Wi-Fi provisioning.

Protocol (JSON lines):
- {"cmd":"PING"}
- {"cmd":"SCAN_WIFI"}
- {"cmd":"CONNECT_WIFI","ssid":"MySSID","password":"mypw"}
Responses are JSON objects followed by newline.
"""

import json
import subprocess
import traceback
import sys
import time
from app import utils

# PyBluez:
from bluetooth import BluetoothSocket, RFCOMM, PORT_ANY, advertise_service, SERIAL_PORT_CLASS, SERIAL_PORT_PROFILE

SERVICE_NAME = "MedicamProvision"
UUID = "00001101-0000-1000-8000-00805F9B34FB"  # SPP UUID

def scan_wifi():
    """Возвращает список сетей через nmcli."""
    try:
        # формат: SSID:SIGNAL
        cmd = ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi"]
        out = subprocess.check_output(cmd, text=True, errors="ignore")
        nets = []
        seen = set()
        for line in out.splitlines():
            if not line.strip():
                continue
            parts = line.split(":", 1)
            ssid = parts[0].strip()
            signal = parts[1].strip() if len(parts) > 1 else ""
            if ssid and ssid not in seen:
                nets.append({"ssid": ssid, "signal": signal})
                seen.add(ssid)
        return {"networks": nets}
    except Exception as e:
        return {"error": str(e)}

def connect_wifi(ssid: str, password: str, timeout: int = 30):
    """Пытается подключиться к Wi-Fi через nmcli. Возвращает dict с результатом."""
    try:
        # nmcli может потребовать привилегий; обычно работает от пользователя с NetworkManager.
        if password:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid, "password", password]
        else:
            cmd = ["nmcli", "dev", "wifi", "connect", ssid]
        proc = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
        success = proc.returncode == 0
        out = proc.stdout.strip()
        err = proc.stderr.strip()
        result = {
            "success": success,
            "stdout": out,
            "stderr": err
        }
        if success:
            # пометим provisioned и запишем basic info
            # читаем local IP (если есть)
            try:
                ip = subprocess.check_output(["hostname", "-I"], text=True).strip().split()[0]
            except Exception:
                ip = ""
            utils.set_provisioned(True, {"ssid": ssid, "ip": ip})
        return result
    except Exception as e:
        return {"success": False, "error": str(e)}

def handle_client(client_sock):
    try:
        # читаем до первой новой строки
        data = b""
        client_sock.settimeout(60)
        while True:
            chunk = client_sock.recv(1024)
            if not chunk:
                break
            data += chunk
            if b"\n" in chunk:
                break
        if not data:
            return
        msg = data.decode("utf-8", errors="ignore").strip()
        # ожидаем JSON
        try:
            payload = json.loads(msg)
        except Exception:
            # если не json, отправим ошибку
            resp = {"error": "invalid_json", "received": msg}
            client_sock.send((json.dumps(resp) + "\n").encode("utf-8"))
            return

        cmd = payload.get("cmd", "").upper()
        if cmd == "PING":
            client_sock.send((json.dumps({"status": "OK"}) + "\n").encode("utf-8"))
        elif cmd == "SCAN_WIFI":
            resp = scan_wifi()
            client_sock.send((json.dumps(resp) + "\n").encode("utf-8"))
        elif cmd == "CONNECT_WIFI":
            ssid = payload.get("ssid", "")
            password = payload.get("password", "")
            if not ssid:
                client_sock.send((json.dumps({"success": False, "error": "missing_ssid"}) + "\n").encode("utf-8"))
            else:
                resp = connect_wifi(ssid, password)
                client_sock.send((json.dumps(resp) + "\n").encode("utf-8"))
        elif cmd == "STATUS":
            client_sock.send((json.dumps({"provisioned": utils.is_provisioned(), "info": utils.get_provision_info()}) + "\n").encode("utf-8"))
        else:
            client_sock.send((json.dumps({"error": "unknown_cmd", "cmd": cmd}) + "\n").encode("utf-8"))
    except Exception:
        tb = traceback.format_exc()
        try:
            client_sock.send((json.dumps({"error": "exception", "trace": tb}) + "\n").encode("utf-8"))
        except Exception:
            pass
    finally:
        try:
            client_sock.close()
        except Exception:
            pass

def run_server():
    server_sock = BluetoothSocket(RFCOMM)
    server_sock.bind(("", PORT_ANY))
    server_sock.listen(1)
    port = server_sock.getsockname()[1]
    advertise_service(server_sock, SERVICE_NAME,
                      service_id=UUID,
                      service_classes=[UUID, SERIAL_PORT_CLASS],
                      profiles=[SERIAL_PORT_PROFILE])
    print(f"Bluetooth provisioning server started on RFCOMM port {port}")
    try:
        while True:
            try:
                client_sock, client_info = server_sock.accept()
                print(f"Accepted connection from {client_info}")
                handle_client(client_sock)
            except KeyboardInterrupt:
                break
            except Exception as e:
                print(f"Error accepting/handling client: {e}")
                time.sleep(1)
    finally:
        try:
            server_sock.close()
        except Exception:
            pass

if __name__ == "__main__":
    run_server()
