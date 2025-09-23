import asyncio
import json
import subprocess
from pathlib import Path
from bleak.backends.peripheral import BleakPeripheral

# UUID для сервиса и характеристик (можно сгенерировать через uuidgen)
SERVICE_UUID = "12345678-1234-5678-1234-56789abcdef0"
CMD_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef1"
RESP_CHAR_UUID = "12345678-1234-5678-1234-56789abcdef2"

PROVISION_FILE = Path("/home/radxa/medicam-server/provision.json")


class ProvisionPeripheral(BleakPeripheral):
    def __init__(self):
        super().__init__(SERVICE_UUID, "MedicamProvision")

        self.command_char = self.add_characteristic(CMD_CHAR_UUID, ["write"])
        self.response_char = self.add_characteristic(RESP_CHAR_UUID, ["read", "notify"])

        self.command_char.set_write_callback(self.on_command)

    async def on_command(self, value: bytearray):
        try:
            data = json.loads(value.decode())
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
        await self.response_char.write_value(resp_json, True)  # notify

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


async def main():
    peripheral = ProvisionPeripheral()
    await peripheral.start()
    print("Provisioning BLE service started")
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
