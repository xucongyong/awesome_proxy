import json
import logging
import os
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("export_to_postgres")

def export_to_postgres(json_file: str = "nodes_backup.json", sql_file: str = "postgres_nodes.sql"):
    path = Path(json_file)
    if not path.exists():
        logger.error(f"Backup file not found: {json_file}")
        return

    with open(path, "r", encoding="utf-8") as f:
        nodes = json.load(f)

    logger.info(f"Loaded {len(nodes)} nodes from {json_file}. Generating {sql_file} for PostgreSQL...")

    with open(sql_file, "w", encoding="utf-8") as out:
        out.write("""-- PostgreSQL Schema & Data for Awesome Proxy Pool
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

ALTER TABLE nodes DROP CONSTRAINT IF EXISTS nodes_node_url_key;
CREATE UNIQUE INDEX IF NOT EXISTS idx_nodes_node_url_hash ON nodes (md5(node_url));

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS source_url TEXT;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cn_delay_ms INTEGER DEFAULT -1;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cn_is_active INTEGER DEFAULT NULL;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cn_last_tested TIMESTAMP;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS global_delay_ms INTEGER DEFAULT -1;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS global_is_active INTEGER DEFAULT NULL;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS global_last_tested TIMESTAMP;

CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
CREATE INDEX IF NOT EXISTS idx_nodes_cn_active ON nodes(cn_is_active);
CREATE INDEX IF NOT EXISTS idx_nodes_global_active ON nodes(global_is_active);
CREATE INDEX IF NOT EXISTS idx_nodes_cn_tested ON nodes(cn_last_tested);
CREATE INDEX IF NOT EXISTS idx_nodes_global_tested ON nodes(global_last_tested);

""")
        for n in nodes:
            def sql_val(val):
                if val is None:
                    return "NULL"
                if isinstance(val, (int, float)):
                    return str(val)
                s = str(val).replace("'", "''").replace("\\", "\\\\")
                return f"E'{s}'"

            cols = [
                "id", "node_url", "protocol", "first_seen", "last_tested", "status",
                "delay_ms", "speed_mbps", "fail_count", "is_active", "source_url",
                "cn_delay_ms", "cn_is_active", "cn_last_tested",
                "global_delay_ms", "global_is_active", "global_last_tested"
            ]
            vals = [
                sql_val(n.get("id")),
                sql_val(n.get("node_url")),
                sql_val(n.get("protocol")),
                sql_val(n.get("first_seen")),
                sql_val(n.get("last_tested")),
                sql_val(n.get("status")),
                sql_val(n.get("delay_ms")),
                sql_val(n.get("speed_mbps")),
                sql_val(n.get("fail_count")),
                sql_val(n.get("is_active")),
                sql_val(n.get("source_url")),
                sql_val(n.get("cn_delay_ms")),
                sql_val(n.get("cn_is_active")),
                sql_val(n.get("cn_last_tested")),
                sql_val(n.get("global_delay_ms")),
                sql_val(n.get("global_is_active")),
                sql_val(n.get("global_last_tested"))
            ]
            out.write(f"INSERT INTO nodes ({', '.join(cols)}) VALUES ({', '.join(vals)}) ON CONFLICT ((md5(node_url))) DO UPDATE SET cn_is_active = EXCLUDED.cn_is_active, global_is_active = EXCLUDED.global_is_active, status = EXCLUDED.status;\n")

        out.write("SELECT setval(pg_get_serial_sequence('nodes', 'id'), COALESCE(max(id)+1, 1), false) FROM nodes;\n")

    logger.info(f"Generated {sql_file} successfully! Run 'psql -d <your_db> -f {sql_file}' to import.")

if __name__ == "__main__":
    export_to_postgres()
