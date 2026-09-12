import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tester import build_singbox_test_config, check_tcp_reachable, get_free_port

class TestTester(unittest.TestCase):

    def test_get_free_port(self):
        port1 = get_free_port()
        port2 = get_free_port()
        self.assertGreater(port1, 1024)
        self.assertGreater(port2, 1024)

    def test_check_tcp_reachable_failure(self):
        # Testing a non-existent port on loopback should quickly return False
        self.assertFalse(check_tcp_reachable("127.0.0.1", 59999, timeout=0.2))

    def test_build_singbox_test_config(self):

        outbound = {
            "type": "shadowsocks",
            "tag": "proxy",
            "server": "1.1.1.1",
            "server_port": 8388,
            "method": "aes-256-gcm",
            "password": "pass",
        }
        cfg = build_singbox_test_config(outbound, port=20899)
        self.assertIn("inbounds", cfg)
        self.assertIn("outbounds", cfg)
        self.assertEqual(cfg["inbounds"][0]["listen_port"], 20899)
        self.assertEqual(cfg["outbounds"][0]["tag"], "proxy")


if __name__ == "__main__":
    unittest.main()
