#!/usr/bin/env python3
import json
import base64
import subprocess
import time
from pathlib import Path
import os
import sys
import threading
import traceback
import hashlib
import hmac
import secrets
from concurrent.futures import ThreadPoolExecutor

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

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

SERVICE_UUID = "3f7d1000-6f4b-4e21-9a63-7b9c3f1d0001"
CMD_CHAR_UUID = "3f7d1001-6f4b-4e21-9a63-7b9c3f1d0001"
RESP_CHAR_UUID = "3f7d1002-6f4b-4e21-9a63-7b9c3f1d0001"
PROTOCOL_VERSION = 5
MAX_COMMAND_BYTES = 8192
PAIRING_NONCE_TTL_SECONDS = 120
PAIRING_SESSION_TTL_SECONDS = 300
PAIRING_FAILURE_LIMIT = 5
PAIRING_BLOCK_SECONDS = 60
MAX_PENDING_COMMANDS = 4
MAX_PENDING_RESPONSES = 8
MAX_RESPONSE_BYTES = 8192
MAX_NOTIFICATION_VALUE_BYTES = 150
NOTIFICATION_BODY_BYTES = 120

PROVISION_FILE = utils._provision_path()

TEST_MODE = os.getenv("MEDICAM_BLE_TEST_MODE", "").lower() in {
    "1",
    "true",
    "yes",
}


def encode_notification_frames(payload: bytes, frame_id: str | None = None):
    """Return ATT-safe protocol-5 notifications for one bounded response."""
    if len(payload) > MAX_RESPONSE_BYTES:
        payload = b'{"error":"response_too_large"}'
    if len(payload) <= MAX_NOTIFICATION_VALUE_BYTES:
        return [payload]
    frame_id = frame_id or secrets.token_hex(4)
    total = (len(payload) + NOTIFICATION_BODY_BYTES - 1) // NOTIFICATION_BODY_BYTES
    frames = []
    for sequence in range(total):
        body = payload[
            sequence * NOTIFICATION_BODY_BYTES:
            (sequence + 1) * NOTIFICATION_BODY_BYTES
        ]
        frame = f"M5|{frame_id}|{sequence}|{total}|".encode("ascii") + body
        if len(frame) > MAX_NOTIFICATION_VALUE_BYTES:
            raise ValueError("notification_frame_too_large")
        frames.append(frame)
    return frames

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


def configure_always_on_adapter(dongle) -> None:
    """Keep LE advertisements connectable without enabling Bluetooth pairing."""
    powered_type = type(dongle.powered)
    timeout_type = type(dongle.discoverabletimeout)
    pairable_type = type(dongle.pairable)
    discoverable_type = type(dongle.discoverable)
    dongle.powered = powered_type(True)
    dongle.discoverabletimeout = timeout_type(0)
    dongle.pairable = pairable_type(False)
    dongle.discoverable = discoverable_type(True)


def add_characteristic(periph, **kwargs):
    """Add and return a characteristic across Bluezero API variants.

    Bluezero 0.9.1 appends the created object to ``characteristics`` but
    returns ``None``.  Some forks return the object directly.  Capturing both
    behaviours is essential for server-initiated response notifications.
    """
    characteristics = getattr(periph, "characteristics", None)
    previous_count = len(characteristics) if characteristics is not None else 0
    try:
        characteristic = periph.add_characteristic(**kwargs)
    except TypeError:
        # Older Bluezero variants do not accept notify_callback.
        fallback_kwargs = dict(kwargs)
        fallback_kwargs.pop("notify_callback", None)
        characteristic = periph.add_characteristic(**fallback_kwargs)

    if characteristic is not None:
        return characteristic

    characteristics = getattr(periph, "characteristics", None)
    if characteristics is not None and len(characteristics) > previous_count:
        return characteristics[-1]
    return None

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

        device_name = utils.get_device_name()
        self.periph = peripheral.Peripheral(
            adapter_address=mac,
            local_name=device_name,
        )
        # CoreBluetooth can use the GAP adapter alias instead of the extended
        # advertisement LocalName. Keep both identities product-specific so a
        # new owner never sees the Linux hostname (for example, "nom").
        self.periph.dongle.alias = device_name
        configure_always_on_adapter(self.periph.dongle)

        print("[BLE] Peripheral created via bluezero")

        SRV_ID = 1
        self.periph.add_service(SRV_ID, SERVICE_UUID, True)

        # Command characteristic (write). Bluezero 0.9.1 returns None from
        # add_characteristic(), so capture the object appended to its list.
        self.cmd_char = add_characteristic(
            self.periph,
            srv_id=SRV_ID,
            chr_id=1,
            uuid=CMD_CHAR_UUID,
            value=[],
            notifying=False,
            flags=["write-without-response", "write"],
            read_callback=None,
            write_callback=self.on_command,
            notify_callback=None,
        )

        # Response characteristic (read + notify)
        self.resp_char = add_characteristic(
            self.periph,
            srv_id=SRV_ID,
            chr_id=2,
            uuid=RESP_CHAR_UUID,
            value=[],
            notifying=False,
            flags=["read", "notify"],
            read_callback=self.on_read_response,
            write_callback=None,
            notify_callback=None,
        )
        if self.resp_char is None or not hasattr(self.resp_char, "set_value"):
            raise RuntimeError("Bluezero response characteristic is unavailable")

        self.srv_id = SRV_ID
        self.resp_chr_id = 2
        self.response_value = b'{}'
        # lock to protect response_value between threads
        self._resp_lock = threading.Lock()
        # buffer for fragmented incoming writes (persistent across calls)
        self._cmd_buffer = bytearray()
        self._executor = ThreadPoolExecutor(
            max_workers=2,
            thread_name_prefix="medicam-ble-command",
        )
        self._notify_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="medicam-ble-response",
        )
        self._command_slots = threading.BoundedSemaphore(MAX_PENDING_COMMANDS)
        self._notify_slots = threading.BoundedSemaphore(MAX_PENDING_RESPONSES)
        self._pairing_lock = threading.Lock()
        self._pairing_nonce = ""
        self._pairing_nonce_issued = 0.0
        self._pairing_failures = 0
        self._pairing_blocked_until = 0.0
        self._session_id = ""
        self._session_key = ""
        self._session_expires = 0.0
        self._session_counter = -1
        self._rotate_pairing_nonce_locked()

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
        if len(payload) > MAX_RESPONSE_BYTES:
            payload = json.dumps(
                {
                    "error": "response_too_large",
                    **({"request_id": request_id} if request_id else {}),
                }
            ).encode()

        with self._resp_lock:
            self.response_value = payload

        for frame in encode_notification_frames(payload):
            self._notify_value(frame)
            if len(payload) > MAX_NOTIFICATION_VALUE_BYTES:
                time.sleep(0.015)

    def _notify_value(self, value):
        """Publish one MTU-bounded notification value without logging data."""
        value_list = list(value)
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
            print("[WARN] BLE notify block error:", e)

    def _set_response_async(self, response_dict, request_id=None):
        if not self._notify_slots.acquire(blocking=False):
            return

        def run_response():
            try:
                self._set_response(response_dict, request_id=request_id)
            finally:
                self._notify_slots.release()

        self._notify_executor.submit(run_response)

    def _dispatch_command(self, data):
        """
        Handle commands outside BlueZ's WriteValue callback.

        Calling _set_response() synchronously from WriteValue can make BlueZ
        wait on a nested D-Bus characteristic update before it sends the ATT
        Write Response. iOS then times out the write. Returning from the write
        callback immediately keeps the GATT transaction healthy.
        """
        if not self._command_slots.acquire(blocking=False):
            self._set_response_async({"error": "busy"}, request_id=data.get("request_id"))
            return

        def run_command():
            try:
                self._handle_command(data)
            finally:
                self._command_slots.release()

        self._executor.submit(run_command)

    def _rotate_pairing_nonce_locked(self):
        self._pairing_nonce = secrets.token_urlsafe(24)
        self._pairing_nonce_issued = time.monotonic()

    def _public_pairing_nonce(self):
        with self._pairing_lock:
            if (
                not self._pairing_nonce
                or time.monotonic() - self._pairing_nonce_issued
                >= PAIRING_NONCE_TTL_SECONDS
            ):
                self._rotate_pairing_nonce_locked()
            return self._pairing_nonce

    def _status_payload(self):
        capabilities = [
            "physical_pairing_code",
            "owner_token_recovery",
            "mutual_pairing_proof",
            "session_hmac",
            "client_generated_token",
            "tls_pinning",
            "wifi_scan",
            "wifi_connect",
            "recovery_window",
        ]
        return {
            "status": "ok",
            "protocol": PROTOCOL_VERSION,
            "device": "Medicam",
            "device_id": utils.get_device_id(),
            "device_name": utils.get_device_name(),
            "provisioned": utils.is_provisioned(),
            "pairing_nonce": self._public_pairing_nonce(),
            "pairing_nonce_ttl": PAIRING_NONCE_TTL_SECONDS,
            "capabilities": capabilities,
        }

    @staticmethod
    def _canonical_session_command(data):
        authenticated = {
            key: value
            for key, value in data.items()
            if key != "auth"
        }
        return json.dumps(
            authenticated,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _unlock_pairing(self, data, request_id=None):
        nonce = data.get("nonce")
        proof = data.get("proof")
        now = time.monotonic()
        device_id = utils.get_device_id()
        fingerprint = utils.get_tls_fingerprint()
        with self._pairing_lock:
            if now < self._pairing_blocked_until:
                self._set_response(
                    {
                        "error": "pairing_throttled",
                        "retry_after": max(1, int(self._pairing_blocked_until - now)),
                    },
                    request_id=request_id,
                )
                return
            valid_nonce = (
                isinstance(nonce, str)
                and hmac.compare_digest(nonce, self._pairing_nonce)
                and now - self._pairing_nonce_issued < PAIRING_NONCE_TTL_SECONDS
            )
            valid_proof = bool(
                valid_nonce
                and isinstance(proof, str)
                and utils.verify_pairing_client_proof(nonce, device_id, proof)
            )
            if not valid_proof or not fingerprint:
                self._pairing_failures += 1
                if self._pairing_failures >= PAIRING_FAILURE_LIMIT:
                    self._pairing_blocked_until = now + PAIRING_BLOCK_SECONDS
                    self._pairing_failures = 0
                self._rotate_pairing_nonce_locked()
                self._set_response(
                    {
                        "error": "invalid_pairing_proof",
                        "pairing_nonce": self._pairing_nonce,
                    },
                    request_id=request_id,
                )
                return

            self._pairing_failures = 0
            self._pairing_blocked_until = 0.0
            self._session_id = secrets.token_urlsafe(18)
            self._session_key = utils.pairing_session_key(nonce, device_id)
            self._session_expires = now + PAIRING_SESSION_TTL_SECONDS
            self._session_counter = -1
            server_proof = utils.pairing_server_proof(
                nonce,
                device_id,
                fingerprint,
            )
            self._rotate_pairing_nonce_locked()

        self._set_response(
            {
                "status": "unlocked",
                "auth_method": "physical_code",
                "session_id": self._session_id,
                "expires_in": PAIRING_SESSION_TTL_SECONDS,
                "device_id": device_id,
                "device_name": utils.get_device_name(),
                "tls_fingerprint": fingerprint,
                "server_proof": server_proof,
            },
            request_id=request_id,
        )

    def _unlock_owner(self, data, request_id=None):
        """Unlock Wi-Fi settings using the token already owned by this phone.

        The token itself never crosses BLE. Always-on advertising is safe here
        because a fresh nonce-bound proof is still mandatory and rate-limited.
        """
        if not utils.is_provisioned():
            self._set_response(
                {"error": "owner_recovery_unavailable"},
                request_id=request_id,
            )
            return

        nonce = data.get("nonce")
        proof = data.get("proof")
        now = time.monotonic()
        device_id = utils.get_device_id()
        fingerprint = utils.get_tls_fingerprint()
        with self._pairing_lock:
            if now < self._pairing_blocked_until:
                self._set_response(
                    {
                        "error": "pairing_throttled",
                        "retry_after": max(1, int(self._pairing_blocked_until - now)),
                    },
                    request_id=request_id,
                )
                return
            valid_nonce = (
                isinstance(nonce, str)
                and hmac.compare_digest(nonce, self._pairing_nonce)
                and now - self._pairing_nonce_issued < PAIRING_NONCE_TTL_SECONDS
            )
            valid_proof = bool(
                valid_nonce
                and isinstance(proof, str)
                and utils.verify_owner_pairing_client_proof(
                    nonce,
                    device_id,
                    proof,
                )
            )
            if not valid_proof or not fingerprint:
                self._pairing_failures += 1
                if self._pairing_failures >= PAIRING_FAILURE_LIMIT:
                    self._pairing_blocked_until = now + PAIRING_BLOCK_SECONDS
                    self._pairing_failures = 0
                self._rotate_pairing_nonce_locked()
                self._set_response(
                    {
                        "error": "invalid_owner_proof",
                        "pairing_nonce": self._pairing_nonce,
                    },
                    request_id=request_id,
                )
                return

            self._pairing_failures = 0
            self._pairing_blocked_until = 0.0
            self._session_id = secrets.token_urlsafe(18)
            self._session_key = utils.owner_pairing_session_key(nonce, device_id)
            self._session_expires = now + PAIRING_SESSION_TTL_SECONDS
            self._session_counter = -1
            server_proof = utils.owner_pairing_server_proof(
                nonce,
                device_id,
                fingerprint,
            )
            self._rotate_pairing_nonce_locked()

        self._set_response(
            {
                "status": "unlocked",
                "auth_method": "owner_token",
                "session_id": self._session_id,
                "expires_in": PAIRING_SESSION_TTL_SECONDS,
                "device_id": device_id,
                "device_name": utils.get_device_name(),
                "tls_fingerprint": fingerprint,
                "server_proof": server_proof,
            },
            request_id=request_id,
        )

    def _verify_session_command(self, data):
        session_id = data.get("session_id")
        counter = data.get("counter")
        supplied = data.get("auth")
        with self._pairing_lock:
            if (
                not self._session_id
                or time.monotonic() >= self._session_expires
                or not isinstance(session_id, str)
                or not hmac.compare_digest(session_id, self._session_id)
                or not isinstance(counter, int)
                or counter <= self._session_counter
                or not isinstance(supplied, str)
            ):
                return False
            expected = hmac.new(
                bytes.fromhex(self._session_key),
                self._canonical_session_command(data),
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(supplied, expected):
                return False
            self._session_counter = counter
            return True

    @staticmethod
    def _secret_command_aad(data):
        metadata = {
            key: data.get(key)
            for key in ("cmd", "session_id", "counter", "request_id")
        }
        return json.dumps(
            metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def _decrypt_wifi_credentials(self, data):
        sealed = data.get("sealed")
        if not isinstance(sealed, str) or not sealed:
            raise ValueError("missing_encrypted_credentials")
        try:
            padding = "=" * (-len(sealed) % 4)
            combined = base64.urlsafe_b64decode(sealed + padding)
            if len(combined) < 12 + 16:
                raise ValueError("encrypted_credentials_too_short")
            with self._pairing_lock:
                session_key = bytes.fromhex(self._session_key)
            cleartext = AESGCM(session_key).decrypt(
                combined[:12],
                combined[12:],
                self._secret_command_aad(data),
            )
            credentials = json.loads(cleartext.decode("utf-8"))
        except Exception as error:
            raise ValueError("invalid_encrypted_credentials") from error
        if not isinstance(credentials, dict):
            raise ValueError("invalid_encrypted_credentials")
        ssid = credentials.get("ssid")
        password = credentials.get("password")
        if not isinstance(ssid, str) or not ssid or not isinstance(password, str):
            raise ValueError("invalid_encrypted_credentials")
        return ssid, password

    def _clear_session(self):
        with self._pairing_lock:
            self._session_id = ""
            self._session_key = ""
            self._session_expires = 0.0
            self._session_counter = -1

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

    def _worker_connect_wifi(
        self,
        ssid,
        password,
        api_token=None,
        request_id=None,
    ):
        try:
            result = self.connect_wifi(ssid, password)
            ok = bool(result.get("ok"))
            ip = result.get("ip", "")
            if ok:
                try:
                    utils.set_provisioned(
                        True,
                        {"ssid": ssid, "ip": ip},
                        api_token=api_token,
                    )
                except OSError as error:
                    print(
                        "[ERR] worker_connect_wifi: could not persist owner state:",
                        error,
                        traceback.format_exc(),
                    )
                    self._set_response(
                        {"error": "provision_state_write_failed"},
                        request_id=request_id,
                    )
                    return
                self._set_response(
                    {
                        "status": "connected",
                        "ip": ip,
                        "device_id": utils.get_device_id(),
                        "device_name": utils.get_device_name(),
                        "tls_fingerprint": utils.get_tls_fingerprint(),
                    },
                    request_id=request_id,
                )
                self._clear_session()
            else:
                self._set_response(
                    {
                        "status": "failed",
                        "error_code": result.get("error_code", "connection_failed"),
                    },
                    request_id=request_id,
                )
        except Exception as e:
            print("[ERR] worker_connect_wifi:", e, traceback.format_exc())
            self._set_response(
                {"error": "wifi_connection_failed"},
                request_id=request_id,
            )

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

        if cmd == "UNLOCK":
            self._unlock_pairing(data, request_id=request_id)
            return

        if cmd == "OWNER_UNLOCK":
            self._unlock_owner(data, request_id=request_id)
            return

        if cmd == "SCAN_WIFI":
            if not self._verify_session_command(data):
                self._set_response(
                    {"error": "pairing_required"},
                    request_id=request_id,
                )
                return
            self._worker_scan_wifi(request_id=request_id)
            return

        if cmd == "CONNECT_WIFI":
            if not self._verify_session_command(data):
                self._set_response(
                    {"error": "pairing_required"},
                    request_id=request_id,
                )
                return
            try:
                ssid, password = self._decrypt_wifi_credentials(data)
            except ValueError:
                self._set_response(
                    {"error": "invalid_encrypted_credentials"},
                    request_id=request_id,
                )
                return
            try:
                api_token = utils.derive_owner_api_token(
                    self._session_key,
                    self._session_id,
                    utils.get_device_id(),
                )
            except ValueError:
                self._set_response(
                    {"error": "invalid_pairing_session"},
                    request_id=request_id,
                )
                return

            self._worker_connect_wifi(
                ssid,
                password,
                api_token,
                request_id=request_id,
            )
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
            current_ssid = get_wifi_ssid()
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
                check=True,
                timeout=15,
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
                            "security": security.strip(),
                            "connected": ssid == current_ssid,
                            "supported": not any(
                                marker in security.upper()
                                for marker in ("802.1X", "EAP")
                            ),
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
        finally:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._notify_executor.shutdown(wait=False, cancel_futures=True)

        # Bluezero's publish call owns the D-Bus event loop and should not
        # return during normal service operation. A clean-looking return would
        # otherwise leave systemd reporting an active process with no GATT
        # advertisement, so force the configured on-failure restart.
        raise RuntimeError("BLE D-Bus event loop stopped unexpectedly")

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
        # systemd Restart=on-failure must see a non-zero result so stale BlueZ
        # registrations and transient publish failures are self-healing.
        raise
