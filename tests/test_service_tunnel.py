import subprocess
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class ServiceTunnelDeploymentTests(unittest.TestCase):
    def test_shell_scripts_have_valid_syntax(self):
        for relative_path in (
            "scripts/medicam_service_tunnel.sh",
            "scripts/install_service_tunnel.sh",
            "scripts/install_lab_maintenance_access.sh",
        ):
            with self.subTest(script=relative_path):
                result = subprocess.run(
                    ["bash", "-n", str(ROOT / relative_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_lab_maintenance_access_is_isolated_and_key_only(self):
        installer = (
            ROOT / "scripts/install_lab_maintenance_access.sh"
        ).read_text(encoding="utf-8")
        self.assertIn('MAINTENANCE_USER="medicam-maint"', installer)
        self.assertIn("restrict %s", installer)
        self.assertIn("Expected exactly one OpenSSH Ed25519 public key", installer)
        self.assertIn("visudo -cf", installer)
        self.assertIn("NOPASSWD: ALL", installer)
        self.assertIn("usermod --password 'x'", installer)
        self.assertNotIn("MAINTENANCE_USER=\"radxa\"", installer)

    def test_tunnel_is_outbound_loopback_only_and_pins_server_key(self):
        script = (ROOT / "scripts/medicam_service_tunnel.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            '-R "127.0.0.1:$TUNNEL_REVERSE_PORT:127.0.0.1:22"',
            script,
        )
        self.assertIn("StrictHostKeyChecking=yes", script)
        self.assertIn("BatchMode=yes", script)
        self.assertIn("ExitOnForwardFailure=yes", script)
        self.assertNotIn("StrictHostKeyChecking=no", script)

    def test_local_ssh_remains_key_only_for_manufacturing_recovery(self):
        firewall = (ROOT / "deploy/nftables/medicam.nft").read_text(
            encoding="utf-8"
        )
        ssh_policy = (ROOT / "deploy/ssh/medicam.conf").read_text(encoding="utf-8")
        self.assertIn("tcp dport 22 accept", firewall)
        self.assertIn("PasswordAuthentication no", ssh_policy)
        self.assertIn("AuthenticationMethods publickey", ssh_policy)

    def test_maintenance_key_is_limited_to_reverse_tunnel_source(self):
        installer = (ROOT / "scripts/install_service_tunnel.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn('from="127.0.0.1"', installer)
        self.assertIn("no-port-forwarding", installer)
        self.assertIn(
            "PasswordAuthentication no",
            (ROOT / "deploy/ssh/medicam.conf").read_text(),
        )
        self.assertIn("usermod --password 'x'", installer)
        self.assertNotIn("usermod --lock", installer)


if __name__ == "__main__":
    unittest.main()
