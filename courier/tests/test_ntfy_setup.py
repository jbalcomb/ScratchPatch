import re
import tempfile
import unittest
from pathlib import Path

from courier.ntfy_setup import new_token, write_config

FAKE_HASH = "$2a$10$abcdefghijklmnopqrstuuABCDEFGHIJKLMNOPQRSTUVWXYZ01234"


class NtfySetupTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        self.hashed = []

    def hasher(self, password):
        self.hashed.append(password)
        return FAKE_HASH

    def test_token_shape(self):
        self.assertRegex(new_token(), r"^tk_[a-z0-9]{29}$")

    def test_config_is_private_and_declares_both_users(self):
        text = write_config(self.dir, self.hasher).read_text(encoding="utf-8")
        gateway = (self.dir / "ntfy-gateway.token").read_text().strip()
        device = (self.dir / "ntfy-device.token").read_text().strip()
        self.assertNotEqual(gateway, device)
        self.assertIn('auth-default-access: "deny-all"', text)
        self.assertIn('web-root: "disable"', text)
        self.assertIn('listen-http: "127.0.0.1:2586"', text)
        self.assertIn('"courier-gateway:*:write-only"', text)
        self.assertIn('"courier-device:*:read-only"', text)
        self.assertIn(f'"courier-gateway:{gateway}:Courier gateway"', text)
        self.assertIn(f'"courier-device:{device}:Courier devices"', text)
        self.assertIn(f'"courier-gateway:{FAKE_HASH}:user"', text)
        self.assertEqual(len(set(self.hashed)), 2, "each user gets its own random password")

    def test_paths_use_forward_slashes(self):
        text = write_config(self.dir, self.hasher).read_text(encoding="utf-8")
        for path in re.findall(r'(?:cache|auth)-file: "([^"]+)"', text):
            self.assertNotIn("\\", path)
            self.assertTrue(path.endswith(".db"))

    def test_rerun_keeps_tokens(self):
        write_config(self.dir, self.hasher)
        first = (self.dir / "ntfy-device.token").read_text()
        write_config(self.dir, self.hasher)
        self.assertEqual((self.dir / "ntfy-device.token").read_text(), first)

    def test_malformed_token_file_is_replaced(self):
        (self.dir / "ntfy-gateway.token").write_text("not-a-token\n")
        write_config(self.dir, self.hasher)
        self.assertRegex((self.dir / "ntfy-gateway.token").read_text().strip(), r"^tk_[a-z0-9]{29}$")

    def test_written_with_lf_only(self):
        self.assertNotIn(b"\r\n", write_config(self.dir, self.hasher).read_bytes())


if __name__ == "__main__":
    unittest.main()
