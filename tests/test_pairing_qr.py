import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "export_pairing_qr.py"
SPEC = importlib.util.spec_from_file_location("export_pairing_qr", SCRIPT)
pairing_qr = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(pairing_qr)


class PairingQrTests(unittest.TestCase):
    def setUp(self):
        self.device_id = "856279C7"
        self.code = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        self.grouped = "ABCD-EFGH-IJKL-MNOP-QRST-UVWX-YZ"
        self.payload = (
            "medicam://pair?device_id=856279C7&code=ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        )
        self.output = (
            f"Device ID: {self.device_id}\r\n"
            f"Pairing code: {self.grouped}\r\n"
            f"QR payload: {self.payload}\r\n"
            "Connection to camera closed.\r\n"
        )

    def test_helper_output_is_parsed_without_printing_secret(self):
        self.assertEqual(
            pairing_qr.parse_pairing_info(self.output),
            (self.device_id, self.code, self.payload),
        )

    def test_payload_must_match_the_separate_identity_fields(self):
        bad_output = self.output.replace("device_id=856279C7", "device_id=00000000")
        with self.assertRaises(pairing_qr.PairingInfoError):
            pairing_qr.parse_pairing_info(bad_output)

    def test_malformed_pairing_code_is_rejected(self):
        bad_output = self.output.replace(self.grouped, "NOT-A-PAIRING-CODE")
        with self.assertRaises(pairing_qr.PairingInfoError):
            pairing_qr.parse_pairing_info(bad_output)

    @unittest.skipUnless(
        importlib.util.find_spec("qrcode") and importlib.util.find_spec("png"),
        "pairing QR tool dependencies are not installed",
    )
    def test_rendered_package_is_private_and_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            output_dir = Path(directory) / "private"
            paths = pairing_qr.render_pairing_package(
                output_dir,
                self.device_id,
                self.code,
                self.payload,
            )
            self.assertEqual(stat.S_IMODE(output_dir.stat().st_mode), 0o700)
            self.assertEqual(set(paths), {"png", "svg", "label", "text"})
            for path in paths.values():
                self.assertTrue(path.is_file())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertGreater(paths["png"].stat().st_size, 500)
            self.assertIn("NOM", paths["label"].read_text())
            self.assertIn(self.grouped, paths["text"].read_text())


if __name__ == "__main__":
    unittest.main()
