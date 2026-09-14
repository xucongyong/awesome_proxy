import json
import logging
import sqlite3
from pathlib import Path
from database import SQLiteDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("import_backup")

def import_backup(json_file: str = "nodes_backup.json", db_file: str = "nodes.db"):
    path = Path(json_file)
    if not path.exists():
        logger.error(f"Backup file not found: {json_file}")
        return

    db = SQLiteDatabase(db_file)
    db.init_db()

    with open(path, "r", encoding="utf-8") as f:
        nodes = json.load(f)

    logger.info(f"Loaded {len(nodes)} nodes from {json_file}. Writing to {db_file}...")

    with sqlite3.connect(db_file) as conn:
        conn.execute("BEGIN TRANSACTION;")
        for n in nodes:
            conn.execute("""
            INSERT OR REPLACE INTO nodes (
                id, node_url, protocol, first_seen, last_tested, status,
                delay_ms, speed_mbps, fail_count, is_active, source_url,
                cn_delay_ms, cn_is_active, cn_last_tested,
                global_delay_ms, global_is_active, global_last_tested
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
            """, (
                n["id"], n["node_url"], n["protocol"], n["first_seen"], n["last_tested"], n["status"],
                n["delay_ms"], n["speed_mbps"], n["fail_count"], n["is_active"], n.get("source_url"),
                n.get("cn_delay_ms"), n.get("cn_is_active"), n.get("cn_last_tested"),
                n.get("global_delay_ms"), n.get("global_is_active"), n.get("global_last_tested")
            ))
        conn.commit()

    stats = db.get_stats()
    logger.info(f"Import completed successfully! Current local database stats: {stats}")

if __name__ == "__main__":
    import_backup()
