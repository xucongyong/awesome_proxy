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

    def test_node_tester_all_nodes(self):
        import tempfile
        from unittest.mock import patch
        from database import SQLiteDatabase
        from tester import NodeTester

        with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
            db = SQLiteDatabase(tmp.name)
            db.init_db()
            db.insert_nodes_batch([
                "vless://u1@example1.com:443",
                "trojan://u2@example2.com:443",
            ])
            tester = NodeTester(db=db, concurrency=2)

            with patch("tester.test_nodes_batch_singbox_clash") as mock_batch:
                mock_batch.return_value = [
                    (1, "active", 100, 0.0, 0),
                    (2, "dead", -1, 0.0, 3),
                ]
                stats = tester.run(enable_speed_test=False, region="cn", all_nodes=True)
                self.assertEqual(stats["tested"], 2)
                self.assertEqual(stats["active"], 1)
                self.assertEqual(stats["dead"], 1)


if __name__ == "__main__":
    unittest.main()
