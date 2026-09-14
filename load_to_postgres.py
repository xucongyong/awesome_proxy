import argparse
import json
import logging
import sys
from pathlib import Path

try:
    import psycopg2
    from psycopg2.extras import execute_batch
except ImportError:
    print("psycopg2 is not installed. Please install with: pip install psycopg2-binary")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("load_to_postgres")

def load_to_postgres(conn_str: str, json_file: str = "nodes_backup.json"):
    path = Path(json_file)
    if not path.exists():
        logger.error(f"Backup file not found: {json_file}")
        return

    logger.info("Connecting to PostgreSQL...")
    try:
        conn = psycopg2.connect(conn_str)
    except Exception as e:
        logger.error(f"Could not connect to PostgreSQL: {e}")
        return

    conn.autocommit = True
    cur = conn.cursor()

    logger.info("Checking and updating PostgreSQL table schema & hash index...")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS nodes (
        id SERIAL PRIMARY KEY,
        node_url TEXT NOT NULL,
        protocol VARCHAR(32) NOT NULL,
        first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        last_tested TIMESTAMP,
        status VARCHAR(32) DEFAULT 'untested',
        is_active INTEGER DEFAULT NULL,
        source_url TEXT,
        delay_ms INTEGER DEFAULT -1,
        speed_mbps REAL DEFAULT 0.0,
        fail_count INTEGER DEFAULT 0,
        cn_delay_ms INTEGER DEFAULT -1,
        cn_is_active INTEGER DEFAULT NULL,
        cn_last_tested TIMESTAMP,
        global_delay_ms INTEGER DEFAULT -1,
        global_is_active INTEGER DEFAULT NULL,
        global_last_tested TIMESTAMP
    );
    """)

    # Drop standard btree constraint on node_url if it exists because URLs > 2704 bytes exceed btree page limit
    try:
        cur.execute("ALTER TABLE nodes DROP CONSTRAINT IF EXISTS nodes_node_url_key;")
    except Exception:
        pass

    # Create safe MD5 hash unique index (never exceeds 32 bytes)
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_node_url_hash ON nodes (md5(node_url));")

    for col_name, col_type in [
        ("source_url", "TEXT"),
        ("cn_delay_ms", "INTEGER DEFAULT -1"),
        ("cn_is_active", "INTEGER DEFAULT NULL"),
        ("cn_last_tested", "TIMESTAMP"),
        ("global_delay_ms", "INTEGER DEFAULT -1"),
        ("global_is_active", "INTEGER DEFAULT NULL"),
        ("global_last_tested", "TIMESTAMP"),
    ]:
        try:
            cur.execute(f"ALTER TABLE nodes ADD COLUMN IF NOT EXISTS {col_name} {col_type};")
        except Exception:
            pass

    cur.execute("CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nodes_cn_active ON nodes(cn_is_active);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nodes_global_active ON nodes(global_is_active);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nodes_cn_tested ON nodes(cn_last_tested);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_nodes_global_tested ON nodes(global_last_tested);")

    with open(path, "r", encoding="utf-8") as f:
        nodes = json.load(f)

    logger.info(f"Loaded {len(nodes)} nodes from {json_file}. Inserting into PostgreSQL...")

    sql = """
    INSERT INTO nodes (
        id, node_url, protocol, first_seen, last_tested, status,
        delay_ms, speed_mbps, fail_count, is_active, source_url,
        cn_delay_ms, cn_is_active, cn_last_tested,
        global_delay_ms, global_is_active, global_last_tested
    ) VALUES (
        %(id)s, %(node_url)s, %(protocol)s, %(first_seen)s, %(last_tested)s, %(status)s,
        %(delay_ms)s, %(speed_mbps)s, %(fail_count)s, %(is_active)s, %(source_url)s,
        %(cn_delay_ms)s, %(cn_is_active)s, %(cn_last_tested)s,
        %(global_delay_ms)s, %(global_is_active)s, %(global_last_tested)s
    ) ON CONFLICT ((md5(node_url))) DO UPDATE SET
        cn_is_active = COALESCE(EXCLUDED.cn_is_active, nodes.cn_is_active),
        global_is_active = COALESCE(EXCLUDED.global_is_active, nodes.global_is_active),
        status = EXCLUDED.status;
    """

    batch_size = 500
    for i in range(0, len(nodes), batch_size):
        chunk = nodes[i : i + batch_size]
        execute_batch(cur, sql, chunk, page_size=batch_size)
        logger.info(f"Progress: {min(i + batch_size, len(nodes))}/{len(nodes)} nodes processed.")

    try:
        cur.execute("SELECT setval(pg_get_serial_sequence('nodes', 'id'), COALESCE(max(id)+1, 1), false) FROM nodes;")
    except Exception:
        pass

    cur.execute("SELECT count(*) FROM nodes;")
    count = cur.fetchone()[0]
    logger.info(f"All done! Total nodes in PostgreSQL 'nodes' table: {count}")

    cur.close()
    conn.close()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import nodes_backup.json into PostgreSQL")
    parser.add_argument("--url", type=str, default=None, help="Postgres connection string, e.g. postgresql://user:pass@192.168.6.1:5432/postgres")
    parser.add_argument("--host", type=str, default="192.168.6.1", help="Postgres host (default: 192.168.6.1)")
    parser.add_argument("--port", type=int, default=5432, help="Postgres port (default: 5432)")
    parser.add_argument("--user", type=str, default="postgres", help="Postgres user (default: postgres)")
    parser.add_argument("--password", type=str, default="", help="Postgres password")
    parser.add_argument("--dbname", type=str, default="postgres", help="Postgres database name (default: postgres)")
    parser.add_argument("--file", type=str, default="nodes_backup.json", help="Backup json file")

    args = parser.parse_args()

    if args.url:
        conn_str = args.url
    else:
        conn_str = f"postgresql://{args.user}:{args.password}@{args.host}:{args.port}/{args.dbname}"

    load_to_postgres(conn_str, json_file=args.file)
