import base64
import json
import subprocess
import tempfile
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from exporter import generate_singbox_client_config, generate_subscription_base64

class TestExporter(unittest.TestCase):

    def test_generate_subscription_base64(self):
        urls = [
            "vless://u1@1.1.1.1:443",
            "trojan://p1@2.2.2.2:443",
        ]
        b64 = generate_subscription_base64(urls)
        decoded = base64.b64decode(b64).decode("utf-8")
        self.assertEqual(decoded, "vless://u1@1.1.1.1:443\ntrojan://p1@2.2.2.2:443")

    def test_generate_singbox_client_config_validity(self):
        nodes = [
            {
                "id": 1,
                "node_url": "vless://00000000-0000-0000-0000-000000000000@1.1.1.1:443?security=tls&sni=example.com",
                "protocol": "vless",
                "delay_ms": 120,
                "speed_mbps": 25.0,
            },
            {
                "id": 2,
                "node_url": "trojan://mypass@2.2.2.2:443?sni=example.com",
                "protocol": "trojan",
                "delay_ms": 200,
                "speed_mbps": 10.0,
            },
        ]
        cfg = generate_singbox_client_config(nodes, mixed_port=7890)
        self.assertEqual(cfg["inbounds"][0]["listen_port"], 7890)

        # Validate complete configuration with sing-box check command
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            p = f.name
        try:
            res = subprocess.run(
                ["sing-box", "check", "-c", p], capture_output=True, text=True
            )
            self.assertEqual(res.returncode, 0, f"sing-box rejected generated client config: {res.stderr}")
        finally:
            import os
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    unittest.main()
