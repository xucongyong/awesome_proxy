import json
import logging
import sqlite3
import urllib.parse
from typing import Any, Dict, List, Optional
import requests

from config import (
    CLOUDFLARE_ACCOUNT_ID,
    CLOUDFLARE_API_TOKEN,
    CLOUDFLARE_DATABASE_ID,
    FORCE_LOCAL_DB,
    LOCAL_DB_PATH,
)

logger = logging.getLogger("database")

def extract_protocol(url: str) -> str:
    """Extract and normalize protocol prefix from a proxy node URL."""
    try:
        scheme = url.split("://", 1)[0].lower().strip()
        if scheme == "hysteria2":
            return "hy2"
        if scheme == "tuic-v5":
            return "tuic"
        return scheme
    except Exception:
        return "unknown"

class Database:
    """Base abstract interface for database operations."""

    def init_db(self) -> None:
        raise NotImplementedError

    def get_last_pushed_date(self) -> Optional[str]:
        raise NotImplementedError

    def record_fetch_log(self, last_pushed_date: str, nodes_found: int, nodes_added: int) -> None:
        raise NotImplementedError

    def insert_nodes_batch(
        self,
        node_urls: Any,
        source_url: Optional[str] = None,
    ) -> int:
        raise NotImplementedError

    def get_nodes_for_testing(self, limit: int = 50, region: str = "cn") -> List[Dict[str, Any]]:
        raise NotImplementedError

    def update_test_result(
        self,
        node_id: int,
        status: str,
        delay_ms: int,
        speed_mbps: float,
        fail_count: int,
        region: str = "cn",
    ) -> None:
        raise NotImplementedError

    def update_test_results_batch(
        self,
        results: List[Tuple[int, str, int, float, int]],
        region: str = "cn",
    ) -> None:
        for node_id, status, delay_ms, speed_mbps, fail_count in results:
            self.update_test_result(node_id, status, delay_ms, speed_mbps, fail_count, region=region)

    def get_active_nodes(self, limit: int = 50, region: str = "cn") -> List[Dict[str, Any]]:
        raise NotImplementedError

    def get_stats(self) -> Dict[str, Any]:
        raise NotImplementedError

    def clean_dead_nodes(self, days: int = 30) -> int:
        raise NotImplementedError




class SQLiteDatabase(Database):
    """Local SQLite3 database implementation."""

    def __init__(self, db_path: str = LOCAL_DB_PATH):
        self.db_path = db_path
        self._init_tables()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_tables(self) -> None:
        self.init_db()

    def init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS nodes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    node_url TEXT UNIQUE NOT NULL,
                    protocol TEXT NOT NULL,
                    first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_tested TIMESTAMP,
                    status TEXT DEFAULT 'untested',
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
                CREATE TABLE IF NOT EXISTS fetch_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    last_pushed_date TEXT NOT NULL,
                    nodes_found INTEGER DEFAULT 0,
                    nodes_added INTEGER DEFAULT 0,
                    fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
                CREATE INDEX IF NOT EXISTS idx_nodes_delay ON nodes(delay_ms);
                CREATE INDEX IF NOT EXISTS idx_nodes_active ON nodes(is_active);
                """
            )
            for col_def in [
                ("cn_delay_ms", "INTEGER DEFAULT -1"),
                ("cn_is_active", "INTEGER DEFAULT NULL"),
                ("cn_last_tested", "TIMESTAMP"),
                ("global_delay_ms", "INTEGER DEFAULT -1"),
                ("global_is_active", "INTEGER DEFAULT NULL"),
                ("global_last_tested", "TIMESTAMP"),
            ]:
                try:
                    conn.execute(f"ALTER TABLE nodes ADD COLUMN {col_def[0]} {col_def[1]};")
                except Exception:
                    pass
            conn.commit()

    def get_last_pushed_date(self) -> Optional[str]:
        with self._get_connection() as conn:
            cur = conn.execute(
                "SELECT last_pushed_date FROM fetch_logs ORDER BY id DESC LIMIT 1;"
            )
            row = cur.fetchone()
            return row["last_pushed_date"] if row else None

    def record_fetch_log(self, last_pushed_date: str, nodes_found: int, nodes_added: int) -> None:
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO fetch_logs (last_pushed_date, nodes_found, nodes_added)
                VALUES (?, ?, ?)
                """,
                (last_pushed_date, nodes_found, nodes_added),
            )
            conn.commit()

    def insert_nodes_batch(
        self,
        node_urls: Any,
        source_url: Optional[str] = None,
    ) -> int:
        if not node_urls:
            return 0
        added = 0
        with self._get_connection() as conn:
            for item in node_urls:
                if isinstance(item, tuple):
                    url, src = item[0], item[1] or source_url
                else:
                    url, src = item, source_url

                proto = extract_protocol(url)
                cur = conn.execute(
                    """
                    INSERT INTO nodes (node_url, protocol, source_url)
                    VALUES (?, ?, ?)
                    ON CONFLICT(node_url) DO UPDATE SET source_url = excluded.source_url
                    WHERE nodes.source_url IS NULL AND excluded.source_url IS NOT NULL
                    """,
                    (url, proto, src),
                )
                if cur.rowcount > 0:
                    added += 1
            conn.commit()
        return added

    def get_nodes_for_testing(self, limit: int = 50, region: str = "cn") -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if region == "global":
                cur = conn.execute(
                    """
                    SELECT id, node_url, protocol, status, delay_ms, speed_mbps, fail_count, cn_is_active, global_is_active
                    FROM nodes
                    WHERE status IN ('untested', 'active') OR global_is_active IS NULL
                    ORDER BY CASE WHEN global_is_active IS NULL THEN 0 ELSE 1 END,
                             CASE WHEN global_is_active IS NULL THEN id END DESC,
                             global_last_tested ASC NULLS FIRST
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT id, node_url, protocol, status, delay_ms, speed_mbps, fail_count, cn_is_active, global_is_active
                    FROM nodes
                    WHERE (global_is_active = 1 OR global_is_active IS NULL)
                      AND (cn_is_active IS NULL OR cn_is_active = 1 OR status = 'untested')
                    ORDER BY CASE WHEN cn_is_active IS NULL THEN 0 ELSE 1 END,
                             CASE WHEN cn_is_active IS NULL THEN id END DESC,
                             cn_last_tested ASC NULLS FIRST
                    LIMIT ?
                    """,
                    (limit,),
                )
            return [dict(row) for row in cur.fetchall()]

    def update_test_result(
        self,
        node_id: int,
        status: str,
        delay_ms: int,
        speed_mbps: float,
        fail_count: int,
        region: str = "cn",
    ) -> None:
        is_active = 1 if status == "active" else 0
        with self._get_connection() as conn:
            if region == "global":
                conn.execute(
                    """
                    UPDATE nodes
                    SET global_is_active = ?, global_delay_ms = ?, global_last_tested = CURRENT_TIMESTAMP,
                        status = CASE WHEN ? = 0 AND fail_count >= 3 THEN 'dead' ELSE status END,
                        is_active = CASE WHEN ? = 0 AND fail_count >= 3 THEN 0 ELSE is_active END,
                        fail_count = CASE WHEN ? = 0 THEN fail_count + 1 ELSE fail_count END
                    WHERE id = ?
                    """,
                    (is_active, delay_ms, is_active, is_active, is_active, node_id),
                )
            else:
                conn.execute(
                    """
                    UPDATE nodes
                    SET status = ?, is_active = ?, delay_ms = ?, speed_mbps = ?, fail_count = ?, last_tested = CURRENT_TIMESTAMP,
                        cn_is_active = ?, cn_delay_ms = ?, cn_last_tested = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (status, is_active, delay_ms, speed_mbps, fail_count, is_active, delay_ms, node_id),
                )
            conn.commit()

    def update_test_results_batch(
        self,
        results: List[Tuple[int, str, int, float, int]],
        region: str = "cn",
    ) -> None:
        if not results:
            return
        with self._get_connection() as conn:
            if region == "global":
                data = [
                    (1 if s == "active" else 0, d, 1 if s == "active" else 0, 1 if s == "active" else 0, 1 if s == "active" else 0, nid)
                    for (nid, s, d, sp, fc) in results
                ]
                conn.executemany(
                    """
                    UPDATE nodes
                    SET global_is_active = ?, global_delay_ms = ?, global_last_tested = CURRENT_TIMESTAMP,
                        status = CASE WHEN ? = 0 AND fail_count >= 3 THEN 'dead' ELSE status END,
                        is_active = CASE WHEN ? = 0 AND fail_count >= 3 THEN 0 ELSE is_active END,
                        fail_count = CASE WHEN ? = 0 THEN fail_count + 1 ELSE fail_count END
                    WHERE id = ?
                    """,
                    data,
                )
            else:
                data = [
                    (s, 1 if s == "active" else 0, d, sp, fc, 1 if s == "active" else 0, d, nid)
                    for (nid, s, d, sp, fc) in results
                ]
                conn.executemany(
                    """
                    UPDATE nodes
                    SET status = ?, is_active = ?, delay_ms = ?, speed_mbps = ?, fail_count = ?, last_tested = CURRENT_TIMESTAMP,
                        cn_is_active = ?, cn_delay_ms = ?, cn_last_tested = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    data,
                )
            conn.commit()

    def get_active_nodes(self, limit: int = 50, region: str = "cn") -> List[Dict[str, Any]]:
        with self._get_connection() as conn:
            if region == "global":
                cur = conn.execute(
                    """
                    SELECT id, node_url, protocol,
                           COALESCE(global_delay_ms, delay_ms) AS delay_ms, speed_mbps
                    FROM nodes
                    WHERE global_is_active = 1 AND COALESCE(global_delay_ms, delay_ms) > 0
                    ORDER BY delay_ms ASC, speed_mbps DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            else:
                cur = conn.execute(
                    """
                    SELECT id, node_url, protocol,
                           COALESCE(cn_delay_ms, delay_ms) AS delay_ms, speed_mbps
                    FROM nodes
                    WHERE (cn_is_active = 1 OR (cn_is_active IS NULL AND is_active = 1))
                      AND COALESCE(cn_delay_ms, delay_ms) > 0
                    ORDER BY delay_ms ASC, speed_mbps DESC
                    LIMIT ?
                    """,
                    (limit,),
                )
            return [dict(row) for row in cur.fetchall()]


    def get_stats(self) -> Dict[str, Any]:
        with self._get_connection() as conn:
            total = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            active = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'active'").fetchone()[0]
            untested = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'untested'").fetchone()[0]
            dead = conn.execute("SELECT COUNT(*) FROM nodes WHERE status = 'dead'").fetchone()[0]
            return {"total": total, "active": active, "untested": untested, "dead": dead}

    def clean_dead_nodes(self, days: int = 30) -> int:
        with self._get_connection() as conn:
            cur = conn.execute(
                f"DELETE FROM nodes WHERE status = 'dead' AND fail_count >= 3 AND datetime(last_tested) < datetime('now', '-{days} days');"
            )
            deleted = cur.rowcount
            conn.commit()
            return max(0, deleted)



class D1Database(Database):
    """Cloudflare D1 Serverless SQL implementation via Cloudflare v4 REST API."""

    def __init__(self, account_id: str, database_id: str, api_token: str):
        self.account_id = account_id
        self.database_id = database_id
        self.api_token = api_token
        self.base_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{account_id}/d1/database/{database_id}/query"
        )
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {api_token}",
                "Content-Type": "application/json",
            }
        )
        self._init_tables()

    def _execute(self, sql: str, params: Optional[List[Any]] = None) -> Dict[str, Any]:
        """Execute a single or multi-statement SQL on Cloudflare D1."""
        payload: Dict[str, Any] = {"sql": sql}
        if params is not None:
            payload["params"] = params

        resp = self.session.post(self.base_url, json=payload, timeout=20)
        if not resp.ok:
            raise RuntimeError(f"Cloudflare D1 API HTTP Error {resp.status_code}: {resp.text}")

        data = resp.json()
        if not data.get("success"):
            errors = data.get("errors", [])
            raise RuntimeError(f"Cloudflare D1 Query Failed: {json.dumps(errors)}")

        results = data.get("result", [])
        return results[0] if results else {}

    def _query(self, sql: str, params: Optional[List[Any]] = None) -> List[Dict[str, Any]]:
        res = self._execute(sql, params)
        return res.get("results", [])

    def _init_tables(self) -> None:
        self.init_db()

    def init_db(self) -> None:
        sql = """
        CREATE TABLE IF NOT EXISTS nodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_url TEXT UNIQUE NOT NULL,
            protocol TEXT NOT NULL,
            first_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_tested TIMESTAMP,
            status TEXT DEFAULT 'untested',
            is_active INTEGER DEFAULT NULL,
            source_url TEXT,
            delay_ms INTEGER DEFAULT -1,
            speed_mbps REAL DEFAULT 0.0,
            fail_count INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS fetch_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            last_pushed_date TEXT NOT NULL,
            nodes_found INTEGER DEFAULT 0,
            nodes_added INTEGER DEFAULT 0,
            fetched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_nodes_status ON nodes(status);
        CREATE INDEX IF NOT EXISTS idx_nodes_delay ON nodes(delay_ms);
        CREATE INDEX IF NOT EXISTS idx_nodes_active ON nodes(is_active);
        """
        self._execute(sql)
        for col_def in [
            ("cn_delay_ms", "INTEGER DEFAULT -1"),
            ("cn_is_active", "INTEGER DEFAULT NULL"),
            ("cn_last_tested", "TIMESTAMP"),
            ("global_delay_ms", "INTEGER DEFAULT -1"),
            ("global_is_active", "INTEGER DEFAULT NULL"),
            ("global_last_tested", "TIMESTAMP"),
        ]:
            try:
                self._execute(f"ALTER TABLE nodes ADD COLUMN {col_def[0]} {col_def[1]};")
            except Exception:
                pass

    def get_last_pushed_date(self) -> Optional[str]:
        rows = self._query("SELECT last_pushed_date FROM fetch_logs ORDER BY id DESC LIMIT 1;")
        if rows:
            return rows[0].get("last_pushed_date")
        return None

    def record_fetch_log(self, last_pushed_date: str, nodes_found: int, nodes_added: int) -> None:
        self._execute(
            "INSERT INTO fetch_logs (last_pushed_date, nodes_found, nodes_added) VALUES (?, ?, ?);",
            [last_pushed_date, nodes_found, nodes_added],
        )

    def insert_nodes_batch(
        self,
        node_urls: Any,
        source_url: Optional[str] = None,
        chunk_size: int = 25,
    ) -> int:
        if not node_urls:
            return 0

        total_added = 0
        # Insert using multi-row VALUES in chunks
        for i in range(0, len(node_urls), chunk_size):
            chunk = node_urls[i : i + chunk_size]
            placeholders = ", ".join(["(?, ?, ?)"] * len(chunk))
            sql = (
                f"INSERT INTO nodes (node_url, protocol, source_url) VALUES {placeholders} "
                "ON CONFLICT(node_url) DO UPDATE SET source_url = excluded.source_url "
                "WHERE nodes.source_url IS NULL AND excluded.source_url IS NOT NULL;"
            )
            params: List[Any] = []
            for item in chunk:
                if isinstance(item, tuple):
                    url, src = item[0], item[1] or source_url
                else:
                    url, src = item, source_url
                params.extend([url, extract_protocol(url), src])

            res = self._execute(sql, params)
            meta = res.get("meta", {})
            changes = meta.get("changes", 0)
            total_added += changes

        return total_added

    def get_nodes_for_testing(self, limit: int = 50, region: str = "cn") -> List[Dict[str, Any]]:
        if region == "global":
            sql = """
            SELECT id, node_url, protocol, status, delay_ms, speed_mbps, fail_count, cn_is_active, global_is_active
            FROM nodes
            WHERE status IN ('untested', 'active') OR global_is_active IS NULL
            ORDER BY CASE WHEN global_is_active IS NULL THEN 0 ELSE 1 END,
                     CASE WHEN global_is_active IS NULL THEN id END DESC,
                     global_last_tested ASC
            LIMIT ?;
            """
        else:
            sql = """
            SELECT id, node_url, protocol, status, delay_ms, speed_mbps, fail_count, cn_is_active, global_is_active
            FROM nodes
            WHERE (global_is_active = 1 OR global_is_active IS NULL)
              AND (cn_is_active IS NULL OR cn_is_active = 1 OR status = 'untested')
            ORDER BY CASE WHEN cn_is_active IS NULL THEN 0 ELSE 1 END,
                     CASE WHEN cn_is_active IS NULL THEN id END DESC,
                     cn_last_tested ASC
            LIMIT ?;
            """
        return self._query(sql, [limit])

    def update_test_result(
        self,
        node_id: int,
        status: str,
        delay_ms: int,
        speed_mbps: float,
        fail_count: int,
        region: str = "cn",
    ) -> None:
        is_active = 1 if status == "active" else 0
        if region == "global":
            sql = """
            UPDATE nodes
            SET global_is_active = ?, global_delay_ms = ?, global_last_tested = CURRENT_TIMESTAMP,
                status = CASE WHEN ? = 0 AND fail_count >= 3 THEN 'dead' ELSE status END,
                is_active = CASE WHEN ? = 0 AND fail_count >= 3 THEN 0 ELSE is_active END,
                fail_count = CASE WHEN ? = 0 THEN fail_count + 1 ELSE fail_count END
            WHERE id = ?;
            """
            self._execute(sql, [is_active, delay_ms, is_active, is_active, is_active, node_id])
        else:
            sql = """
            UPDATE nodes
            SET status = ?, is_active = ?, delay_ms = ?, speed_mbps = ?, fail_count = ?, last_tested = CURRENT_TIMESTAMP,
                cn_is_active = ?, cn_delay_ms = ?, cn_last_tested = CURRENT_TIMESTAMP
            WHERE id = ?;
            """
            self._execute(sql, [status, is_active, delay_ms, speed_mbps, fail_count, is_active, delay_ms, node_id])

    def update_test_results_batch(
        self,
        results: List[Tuple[int, str, int, float, int]],
        region: str = "cn",
        chunk_size: int = 50,
    ) -> None:
        if not results:
            return
        for i in range(0, len(results), chunk_size):
            chunk = results[i : i + chunk_size]
            statements = []
            for (nid, s, d, sp, fc) in chunk:
                is_active = 1 if s == "active" else 0
                safe_status = "active" if s == "active" else ("dead" if s == "dead" else "untested")
                node_id = int(nid)
                delay_ms = int(d)
                fail_count = int(fc)
                speed_mbps = float(sp)
                if region == "global":
                    statements.append(
                        f"UPDATE nodes SET global_is_active = {is_active}, global_delay_ms = {delay_ms}, global_last_tested = CURRENT_TIMESTAMP, "
                        f"status = CASE WHEN {is_active} = 0 AND fail_count >= 3 THEN 'dead' ELSE status END, "
                        f"is_active = CASE WHEN {is_active} = 0 AND fail_count >= 3 THEN 0 ELSE is_active END, "
                        f"fail_count = CASE WHEN {is_active} = 0 THEN fail_count + 1 ELSE fail_count END "
                        f"WHERE id = {node_id};"
                    )
                else:
                    statements.append(
                        f"UPDATE nodes SET status = '{safe_status}', is_active = {is_active}, delay_ms = {delay_ms}, speed_mbps = {speed_mbps}, "
                        f"fail_count = {fail_count}, last_tested = CURRENT_TIMESTAMP, "
                        f"cn_is_active = {is_active}, cn_delay_ms = {delay_ms}, cn_last_tested = CURRENT_TIMESTAMP "
                        f"WHERE id = {node_id};"
                    )
            sql = "\n".join(statements)
            self._execute(sql)

    def get_active_nodes(self, limit: int = 50, region: str = "cn") -> List[Dict[str, Any]]:
        if region == "global":
            sql = """
            SELECT id, node_url, protocol,
                   COALESCE(global_delay_ms, delay_ms) AS delay_ms, speed_mbps
            FROM nodes
            WHERE global_is_active = 1 AND COALESCE(global_delay_ms, delay_ms) > 0
            ORDER BY delay_ms ASC, speed_mbps DESC
            LIMIT ?;
            """
        else:
            sql = """
            SELECT id, node_url, protocol,
                   COALESCE(cn_delay_ms, delay_ms) AS delay_ms, speed_mbps
            FROM nodes
            WHERE (cn_is_active = 1 OR (cn_is_active IS NULL AND status = 'active'))
              AND COALESCE(cn_delay_ms, delay_ms) > 0
            ORDER BY delay_ms ASC, speed_mbps DESC
            LIMIT ?;
            """
        return self._query(sql, [limit])


    def get_stats(self) -> Dict[str, Any]:
        sql = """
        SELECT
            (SELECT COUNT(*) FROM nodes) AS total,
            (SELECT COUNT(*) FROM nodes WHERE status = 'active') AS active,
            (SELECT COUNT(*) FROM nodes WHERE status = 'untested') AS untested,
            (SELECT COUNT(*) FROM nodes WHERE status = 'dead') AS dead;
        """
        rows = self._query(sql)
        if rows:
            return {
                "total": rows[0].get("total", 0),
                "active": rows[0].get("active", 0),
                "untested": rows[0].get("untested", 0),
                "dead": rows[0].get("dead", 0),
            }
        return {"total": 0, "active": 0, "untested": 0, "dead": 0}

    def clean_dead_nodes(self, days: int = 30) -> int:
        sql = f"DELETE FROM nodes WHERE status = 'dead' AND fail_count >= 3 AND datetime(last_tested) < datetime('now', '-{days} days');"
        res = self._execute(sql)
        meta = res.get("meta", {})
        return meta.get("changes", 0)



def get_database(force_local: bool = False, db_path: Optional[str] = None) -> Database:
    """Factory function to retrieve the configured database instance."""
    if force_local or FORCE_LOCAL_DB:
        logger.info(f"Using SQLite database at {db_path or LOCAL_DB_PATH}")
        return SQLiteDatabase(db_path or LOCAL_DB_PATH)

    if CLOUDFLARE_ACCOUNT_ID and CLOUDFLARE_DATABASE_ID and CLOUDFLARE_API_TOKEN:
        logger.info(f"Using Cloudflare D1 Database ID {CLOUDFLARE_DATABASE_ID}")
        return D1Database(
            account_id=CLOUDFLARE_ACCOUNT_ID,
            database_id=CLOUDFLARE_DATABASE_ID,
            api_token=CLOUDFLARE_API_TOKEN,
        )

    logger.info(f"Cloudflare credentials not fully provided; falling back to local SQLite at {db_path or LOCAL_DB_PATH}")
    return SQLiteDatabase(db_path or LOCAL_DB_PATH)
