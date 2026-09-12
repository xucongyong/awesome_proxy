import base64
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crawler import extract_nodes_from_text

class TestCrawler(unittest.TestCase):

    def test_extract_nodes_from_plain_text(self):
        sample = """
        Here are some free proxies:
        vless://uuid1@1.1.1.1:443?security=tls#node1
        trojan://pass1@2.2.2.2:443#node2
        Some other text
        hy2://pass2@3.3.3.3:443#node3
        Duplicate:
        vless://uuid1@1.1.1.1:443?security=tls#node1
        """
        nodes = extract_nodes_from_text(sample)
        self.assertEqual(len(nodes), 3)
        self.assertIn("vless://uuid1@1.1.1.1:443?security=tls", nodes)
        self.assertIn("trojan://pass1@2.2.2.2:443", nodes)
        self.assertIn("hy2://pass2@3.3.3.3:443", nodes)

    def test_extract_nodes_from_base64_encoded(self):
        raw_urls = "vless://u1@1.1.1.1:443\ntrojan://p1@2.2.2.2:443"
        b64_encoded = base64.b64encode(raw_urls.encode("utf-8")).decode("utf-8")

        nodes = extract_nodes_from_text(b64_encoded)
        self.assertEqual(len(nodes), 2)
        self.assertIn("vless://u1@1.1.1.1:443", nodes)
        self.assertIn("trojan://p1@2.2.2.2:443", nodes)

    def test_generate_historical_time_windows(self):
        from crawler import generate_historical_time_windows
        windows = generate_historical_time_windows(start_year=2021, until_date="2023-01-01")
        self.assertGreater(len(windows), 0)
        # Windows should be ordered from newest to oldest
        self.assertTrue(windows[0][1] <= "2023-01-01")
        self.assertTrue(windows[-1][0] >= "2021-01-01")


if __name__ == "__main__":
    unittest.main()
