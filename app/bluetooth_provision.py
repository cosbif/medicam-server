import json
import subprocess
from pathlib import Path
from bluezero import adapter, peripheral

SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CMD_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
RESP_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef2"

PROVISION_FILE = Path("/home/radxa/medicam-server/provision.json")


class ProvisionService:
    def __init__(self):
        self.response_value = b'{}'


        # создаём периферию
        adapter_addr = list(adapter.Adapter.available())[0].address
        self.periph = peripheral.Peripheral(adapter_address=adapter_addr, local_name="MedicamProvision")

        # добавляем сервис
        self.periph.add_service(SERVICE_UUID, primary=True)

        # характеристика для команд (write)
        self.periph.add_characteristic(
            service_uuid=SERVICE_UUID,
            uuid=CMD_CHAR_UUID,
            flags=['write'],
            write_callback=self.on_command
        )

        # характеристика для ответа (read+notify)
        self.resp_char = self.periph.add_characteristic(
            service_uuid=SERVICE_UUID,
            uuid=RESP_CHAR_UUID,
            flags=['read', 'notify'],
            read_callback=self.on_read_response
        )

    # Колбэк на чтение ответа
    def on_read_response(self):
        return self.response_value

    # Колбэк на команду
    def on_command(self, value, options):
        try:
            data = json.loads(bytearray(value).decode())
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
                    self.write_provisioned()
                    response = {"status": "connected"}
                else:
                    response = {"status": "failed"}

            else:
                response = {"error": "unknown_command"}

        except Exception as e:
            response = {"error": str(e)}

        resp_json = json.dumps(response).encode()
        self.response_value = resp_json
        self.resp_char.send_notify(resp_json)

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
                    networks.append({"ssid": parts[0], "signal": int(parts[1])})
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

    def write_provisioned(self):
        try:
            data = {"provisioned": True}
            PROVISION_FILE.write_text(json.dumps(data))
            print("Provisioning complete: provision.json updated")
        except Exception as e:
            print(f"write_provisioned error: {e}")

    def run(self):
        print("Starting BLE Provisioning Service...")
        self.peripheral.publish()


if __name__ == "__main__":
    ProvisionService().run()
