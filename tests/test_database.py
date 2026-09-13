import tempfile
import unittest
from unittest.mock import MagicMock, patch
import os
import sys
from pathlib import Path

# Add project root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import D1Database, SQLiteDatabase, extract_protocol

class TestDatabase(unittest.TestCase):

    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(delete=False)
        self.db_path = self.temp_file.name
        self.temp_file.close()
        self.db = SQLiteDatabase(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            try:
                os.remove(self.db_path)
            except Exception:
                pass

    def test_extract_protocol(self):
        self.assertEqual(extract_protocol("vless://123@example.com:443"), "vless")
        self.assertEqual(extract_protocol("hysteria2://pass@example.com:443"), "hy2")
        self.assertEqual(extract_protocol("hy2://pass@example.com:443"), "hy2")
        self.assertEqual(extract_protocol("tuic-v5://pass@example.com:443"), "tuic")
        self.assertEqual(extract_protocol("trojan://pass@example.com:443"), "trojan")

    def test_insert_and_deduplicate(self):
        urls = [
            "vless://user1@example.com:443",
            "trojan://user2@example.com:443",
            "vless://user1@example.com:443",  # duplicate
        ]
        added = self.db.insert_nodes_batch(urls)
        self.assertEqual(added, 2)

        # Inserting duplicate again should add 0
        added_again = self.db.insert_nodes_batch(["vless://user1@example.com:443"])
        self.assertEqual(added_again, 0)

    def test_get_nodes_for_testing_and_update(self):
        urls = [
            "vless://u1@example.com:443",
            "trojan://u2@example.com:443",
        ]
        self.db.insert_nodes_batch(urls)

        nodes = self.db.get_nodes_for_testing(limit=10)
        self.assertEqual(len(nodes), 2)
        node_1 = nodes[0]
        self.assertEqual(node_1["status"], "untested")
        self.assertEqual(node_1["node_url"], "trojan://u2@example.com:443")

        # Update test result
        self.db.update_test_result(
            node_id=node_1["id"],
            status="active",
            delay_ms=120,
            speed_mbps=15.5,
            fail_count=0,
        )

        active_nodes = self.db.get_active_nodes(limit=10)
        self.assertEqual(len(active_nodes), 1)
        self.assertEqual(active_nodes[0]["delay_ms"], 120)

    def test_fetch_logs(self):
        self.assertIsNone(self.db.get_last_pushed_date())
        self.db.record_fetch_log("2026-09-10", nodes_found=50, nodes_added=20)
        self.assertEqual(self.db.get_last_pushed_date(), "2026-09-10")

    @patch("requests.Session.post")
    def test_d1_database_query(self, mock_post):
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.json.return_value = {
            "success": True,
            "result": [{"results": [{"id": 1, "last_pushed_date": "2026-09-11"}]}],
            "errors": [],
        }
        mock_post.return_value = mock_resp

        d1 = D1Database("test_account", "test_db", "test_token")
        last_date = d1.get_last_pushed_date()
        self.assertEqual(last_date, "2026-09-11")


if __name__ == "__main__":
    unittest.main()
