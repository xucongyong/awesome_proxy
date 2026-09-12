import subprocess
import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

class TestMainCLI(unittest.TestCase):

    def test_cli_help(self):
        res = subprocess.run(
            [sys.executable, "main.py", "--help"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--crawl", res.stdout)
        self.assertIn("--test", res.stdout)
        self.assertIn("--export", res.stdout)
        self.assertIn("--local", res.stdout)

    def test_cli_init_db_and_stats_local(self):
        res = subprocess.run(
            [sys.executable, "main.py", "--init-db", "--stats", "--local"],
            capture_output=True,
            text=True,
            cwd=str(Path(__file__).resolve().parent.parent),
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("Database schema initialized successfully", res.stderr)
        self.assertIn("Total Nodes:", res.stdout)


if __name__ == "__main__":
    unittest.main()
