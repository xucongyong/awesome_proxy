import json
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser import parse_hysteria2, parse_node_url, parse_shadowsocks, parse_trojan, parse_vless

class TestParser(unittest.TestCase):

    def _validate_with_singbox(self, outbound: dict):
        cfg = {
            "log": {"level": "warn"},
            "inbounds": [{"type": "mixed", "listen": "127.0.0.1", "listen_port": 20800}],
            "outbounds": [outbound, {"type": "direct", "tag": "direct"}],
        }
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            p = f.name
        try:
            res = subprocess.run(
                ["sing-box", "check", "-c", p], capture_output=True, text=True
            )
            self.assertEqual(res.returncode, 0, f"sing-box rejected config: {res.stderr}")
        finally:
            import os
            if os.path.exists(p):
                os.remove(p)

    def test_parse_vless_reality(self):
        valid_pbk = "YWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWE"
        url = f"vless://00000000-0000-0000-0000-000000000000@1.1.1.1:443?security=reality&sni=example.com&fp=chrome&pbk={valid_pbk}&sid=12345678#test"
        outbound = parse_vless(url, tag="vless-node")
        self.assertIsNotNone(outbound)
        self.assertEqual(outbound["type"], "vless")
        self.assertEqual(outbound["server"], "1.1.1.1")
        self.assertEqual(outbound["server_port"], 443)
        self.assertEqual(outbound["tls"]["reality"]["public_key"], valid_pbk)
        self._validate_with_singbox(outbound)

    def test_parse_trojan(self):
        url = "trojan://password123@example.com:443?sni=example.com#trojan-test"
        outbound = parse_trojan(url, tag="trojan-node")
        self.assertIsNotNone(outbound)
        self.assertEqual(outbound["type"], "trojan")
        self.assertEqual(outbound["password"], "password123")
        self._validate_with_singbox(outbound)

    def test_parse_shadowsocks(self):
        # Base64 of aes-256-gcm:secretpass
        # YWVzLTI1Ni1nY206c2VjcmV0cGFzcw==
        url = "ss://YWVzLTI1Ni1nY206c2VjcmV0cGFzcw==@1.1.1.1:8388#ss-test"
        outbound = parse_shadowsocks(url, tag="ss-node")
        self.assertIsNotNone(outbound)
        self.assertEqual(outbound["type"], "shadowsocks")
        self.assertEqual(outbound["method"], "aes-256-gcm")
        self.assertEqual(outbound["password"], "secretpass")
        self._validate_with_singbox(outbound)

    def test_parse_hysteria2(self):
        url = "hysteria2://mypass@1.1.1.1:443?sni=example.com#hy2-test"
        outbound = parse_hysteria2(url, tag="hy2-node")
        self.assertIsNotNone(outbound)
        self.assertEqual(outbound["type"], "hysteria2")
        self.assertEqual(outbound["password"], "mypass")
        self._validate_with_singbox(outbound)

    def test_parse_node_url_dispatch(self):
        url = "hy2://mypass@1.1.1.1:443?sni=example.com#hy2-test"
        outbound = parse_node_url(url, tag="generic-node")
        self.assertIsNotNone(outbound)
        self.assertEqual(outbound["type"], "hysteria2")


if __name__ == "__main__":
    unittest.main()
