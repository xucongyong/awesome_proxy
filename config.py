import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

def _load_dotenv(path: Path) -> None:
    """Automatically load key-value pairs and export statements from a .env file."""
    if not path.is_file():
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("export "):
                    line = line[len("export ") :].strip()
                if "=" in line:
                    key, val = line.split("=", 1)
                    key = key.strip()
                    val = val.strip().strip('"').strip("'")
                    # Do not overwrite if already explicitly set in system environment
                    if key and key not in os.environ:
                        os.environ[key] = val
    except Exception:
        pass

# Automatically load .env from project root
_load_dotenv(BASE_DIR / ".env")

# GitHub API Credentials
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")

# Cloudflare D1 Credentials
CLOUDFLARE_ACCOUNT_ID = os.getenv("CLOUDFLARE_ACCOUNT_ID", "")
CLOUDFLARE_DATABASE_ID = os.getenv("CLOUDFLARE_DATABASE_ID", "")
CLOUDFLARE_API_TOKEN = os.getenv("CLOUDFLARE_API_TOKEN", "")

# Database mode: force local if requested, or PostgreSQL if DATABASE_URL provided, or CF D1
DATABASE_URL = os.getenv("DATABASE_URL", "")
POSTGRES_SCHEMA = os.getenv("POSTGRES_SCHEMA", "proxy")
FORCE_LOCAL_DB = os.getenv("FORCE_LOCAL_DB", "0").lower() in ("1", "true", "yes")
LOCAL_DB_PATH = os.getenv("LOCAL_DB_PATH", str(BASE_DIR / "nodes.db"))

# Tester settings
SING_BOX_PATH = os.getenv("SING_BOX_PATH", "sing-box")
TEST_URL = os.getenv("TEST_URL", "http://cp.cloudflare.com/generate_204")
SPEED_TEST_URL = os.getenv("SPEED_TEST_URL", "http://speed.cloudflare.com/__down?bytes=5000000")
TEST_TIMEOUT = float(os.getenv("TEST_TIMEOUT", "5.0"))  # seconds (default 5.0 for international links)
MAX_FAIL_COUNT = int(os.getenv("MAX_FAIL_COUNT", "3"))
FAST_TCP_PRECHECK = os.getenv("FAST_TCP_PRECHECK", "1").lower() in ("1", "true", "yes")
TCP_PING_TIMEOUT = float(os.getenv("TCP_PING_TIMEOUT", "2.0"))  # seconds for pre-flight check (default 2.0s)

# Export settings
OUTPUT_DIR = os.getenv("OUTPUT_DIR", str(BASE_DIR / "output"))
DEFAULT_EXPORT_LIMIT = int(os.getenv("DEFAULT_EXPORT_LIMIT", "50"))

