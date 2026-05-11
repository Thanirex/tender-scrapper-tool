import re
import json
import sqlite3
from datetime import datetime
from pathlib import Path


def _normalize(title: str) -> str:
    t = title.lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t


class TenderDB:
    def __init__(self, db_path: Path):
        self._path = str(db_path)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=10, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self):
        with self._connect() as conn:
            # ── Legacy dedup table (unchanged) ─────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS downloaded_tenders (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    title_norm      TEXT    NOT NULL,
                    url             TEXT,
                    site            TEXT,
                    keyword         TEXT,
                    published_date  TEXT,
                    downloaded_at   TEXT    NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_title "
                "ON downloaded_tenders (title_norm)"
            )
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_url "
                "ON downloaded_tenders (url) "
                "WHERE url IS NOT NULL AND url != ''"
            )

            # ── Users ──────────────────────────────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    username      TEXT    NOT NULL UNIQUE,
                    email         TEXT    NOT NULL UNIQUE,
                    password_hash TEXT    NOT NULL,
                    role          TEXT    NOT NULL
                                  CHECK(role IN ('superadmin','admin','user')),
                    created_by    INTEGER REFERENCES users(id),
                    created_at    TEXT    NOT NULL,
                    is_active     INTEGER NOT NULL DEFAULT 1
                )
            """)

            # ── Search sessions ────────────────────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS search_sessions (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER NOT NULL REFERENCES users(id),
                    site         TEXT    NOT NULL,
                    run_date     TEXT    NOT NULL,
                    status       TEXT    NOT NULL DEFAULT 'running',
                    zip_filename TEXT,
                    created_at   TEXT    NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_sessions_date "
                "ON search_sessions (run_date)"
            )

            # ── Keywords per session ───────────────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS session_keywords (
                    id            INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id    INTEGER NOT NULL REFERENCES search_sessions(id),
                    keyword       TEXT    NOT NULL,
                    tenders_found INTEGER NOT NULL DEFAULT 0
                )
            """)

            # ── Found tenders ──────────────────────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS found_tenders (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id     INTEGER NOT NULL REFERENCES search_sessions(id),
                    keyword        TEXT    NOT NULL,
                    title          TEXT    NOT NULL,
                    url            TEXT,
                    site           TEXT    NOT NULL,
                    published_date TEXT,
                    summary_json   TEXT,
                    tender_dir     TEXT,
                    found_at       TEXT    NOT NULL
                )
            """)
            # Migration: add tender_dir to existing databases
            try:
                conn.execute("ALTER TABLE found_tenders ADD COLUMN tender_dir TEXT")
            except sqlite3.OperationalError:
                pass  # column already exists
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_found_session "
                "ON found_tenders (session_id)"
            )

            # ── Tender documents ───────────────────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS tender_documents (
                    id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    tender_id  INTEGER NOT NULL REFERENCES found_tenders(id),
                    filename   TEXT    NOT NULL,
                    file_path  TEXT    NOT NULL,
                    file_type  TEXT,
                    file_size  INTEGER,
                    stored_at  TEXT    NOT NULL
                )
            """)

            # ── Activity logs ──────────────────────────────────────────────
            conn.execute("""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id      INTEGER REFERENCES users(id),
                    username     TEXT,
                    action       TEXT    NOT NULL,
                    details_json TEXT,
                    ip_address   TEXT,
                    timestamp    TEXT    NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_logs_ts "
                "ON activity_logs (timestamp)"
            )

            conn.commit()

    # ── Dedup (unchanged public API) ───────────────────────────────────────

    def is_duplicate(self, title: str, url: str = "") -> bool:
        with self._connect() as conn:
            if url:
                row = conn.execute(
                    "SELECT 1 FROM downloaded_tenders WHERE url = ? LIMIT 1", (url,)
                ).fetchone()
                if row:
                    return True
            norm = _normalize(title)
            row = conn.execute(
                "SELECT 1 FROM downloaded_tenders WHERE title_norm = ? LIMIT 1", (norm,)
            ).fetchone()
            return row is not None

    def mark_downloaded(self, title: str, url: str, site: str,
                        keyword: str, published_date: str = ""):
        norm = _normalize(title)
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO downloaded_tenders
                   (title_norm, url, site, keyword, published_date, downloaded_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (norm, url or "", site or "", keyword or "", published_date or "", now),
            )
            conn.commit()

    # ── User management ────────────────────────────────────────────────────

    def create_user(self, username: str, email: str, password_hash: str,
                    role: str, created_by: int = None) -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO users
                   (username, email, password_hash, role, created_by, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (username, email, password_hash, role, created_by, now),
            )
            conn.commit()
            return cur.lastrowid

    def get_user_by_username(self, username: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()
            return dict(row) if row else None

    def get_user_by_id(self, user_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            return dict(row) if row else None

    def list_users(self, requester_role: str, requester_id: int) -> list[dict]:
        with self._connect() as conn:
            if requester_role == "superadmin":
                rows = conn.execute(
                    """SELECT id, username, email, role, created_at, is_active,
                              created_by
                       FROM users ORDER BY created_at DESC"""
                ).fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, username, email, role, created_at, is_active,
                              created_by
                       FROM users
                       WHERE created_by = ? OR id = ?
                       ORDER BY created_at DESC""",
                    (requester_id, requester_id),
                ).fetchall()
            return [dict(r) for r in rows]

    def update_user(self, user_id: int, **kwargs):
        allowed = {"username", "email", "password_hash", "is_active"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [user_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE users SET {sets} WHERE id = ?", vals)
            conn.commit()

    def superadmin_exists(self) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM users WHERE role='superadmin' LIMIT 1"
            ).fetchone()
            return row is not None

    # ── Search sessions ────────────────────────────────────────────────────

    def create_session(self, user_id: int, site: str) -> int:
        now = datetime.now()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO search_sessions
                   (user_id, site, run_date, status, created_at)
                   VALUES (?, ?, ?, 'running', ?)""",
                (user_id, site, now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds")),
            )
            conn.commit()
            return cur.lastrowid

    def update_session_status(self, session_id: int, status: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE search_sessions SET status = ? WHERE id = ?",
                (status, session_id),
            )
            conn.commit()

    def update_session_zip(self, session_id: int, zip_filename: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE search_sessions SET zip_filename = ? WHERE id = ?",
                (zip_filename, session_id),
            )
            conn.commit()

    def upsert_session_keyword(self, session_id: int, keyword: str, count: int):
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT id FROM session_keywords WHERE session_id = ? AND keyword = ?",
                (session_id, keyword),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE session_keywords SET tenders_found = ? WHERE id = ?",
                    (count, existing["id"]),
                )
            else:
                conn.execute(
                    "INSERT INTO session_keywords (session_id, keyword, tenders_found) "
                    "VALUES (?, ?, ?)",
                    (session_id, keyword, count),
                )
            conn.commit()

    def record_found_tender(self, session_id: int, keyword: str, title: str,
                             url: str, site: str, published_date: str = "",
                             summary: dict = None, tender_dir: str = "") -> int:
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO found_tenders
                   (session_id, keyword, title, url, site, published_date,
                    summary_json, tender_dir, found_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, keyword, title, url or "", site,
                 published_date or "", json.dumps(summary or {}),
                 tender_dir or "", now),
            )
            conn.commit()
            return cur.lastrowid

    # ── Dashboard queries ──────────────────────────────────────────────────

    def get_stats_for_date(self, date_str: str) -> dict:
        with self._connect() as conn:
            session_rows = conn.execute(
                """SELECT s.id, u.username, s.site, s.status, s.zip_filename,
                          s.created_at,
                          GROUP_CONCAT(DISTINCT sk.keyword) AS keywords,
                          COUNT(DISTINCT ft.id)             AS tenders_found
                   FROM search_sessions s
                   JOIN users u ON u.id = s.user_id
                   LEFT JOIN session_keywords sk ON sk.session_id = s.id
                   LEFT JOIN found_tenders ft ON ft.session_id = s.id
                   WHERE s.run_date = ?
                   GROUP BY s.id
                   ORDER BY s.created_at""",
                (date_str,),
            ).fetchall()

            by_site_rows = conn.execute(
                """SELECT s.site, COUNT(DISTINCT ft.id) AS count
                   FROM search_sessions s
                   LEFT JOIN found_tenders ft ON ft.session_id = s.id
                   WHERE s.run_date = ?
                   GROUP BY s.site""",
                (date_str,),
            ).fetchall()

            return {
                "sessions": [dict(r) for r in session_rows],
                "by_site": [dict(r) for r in by_site_rows],
            }

    def get_tenders_for_date(self, date_str: str,
                              site: str = None, keyword: str = None) -> list[dict]:
        with self._connect() as conn:
            q = """SELECT ft.*
                   FROM found_tenders ft
                   JOIN search_sessions s ON s.id = ft.session_id
                   WHERE s.run_date = ?"""
            params: list = [date_str]
            if site:
                q += " AND s.site = ?"
                params.append(site)
            if keyword:
                q += " AND ft.keyword LIKE ?"
                params.append(f"%{keyword}%")
            q += " ORDER BY ft.found_at DESC"
            rows = conn.execute(q, params).fetchall()

        result = []
        for r in rows:
            d = dict(r)
            try:
                d["fields"] = json.loads(d.get("summary_json") or "{}")
            except Exception:
                d["fields"] = {}
            result.append(d)
        return result

    def get_dates_with_data(self) -> list[str]:
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT DISTINCT run_date FROM search_sessions
                   WHERE status = 'complete'
                   ORDER BY run_date DESC LIMIT 90"""
            ).fetchall()
            return [r["run_date"] for r in rows]

    # ── Activity logging ───────────────────────────────────────────────────

    def log_activity(self, user_id: int, username: str, action: str,
                     details: dict = None, ip_address: str = None):
        now = datetime.now().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO activity_logs
                   (user_id, username, action, details_json, ip_address, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, username, action,
                 json.dumps(details or {}), ip_address, now),
            )
            conn.commit()

    def get_activity_logs(self, page: int = 1, limit: int = 50,
                          user_id: int = None) -> dict:
        offset = (page - 1) * limit
        base_q = "FROM activity_logs"
        params: list = []
        if user_id:
            base_q += " WHERE user_id = ?"
            params.append(user_id)

        with self._connect() as conn:
            total = conn.execute(
                f"SELECT COUNT(*) {base_q}", params
            ).fetchone()[0]
            rows = conn.execute(
                f"SELECT * {base_q} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        return {
            "total": total,
            "page": page,
            "limit": limit,
            "items": [dict(r) for r in rows],
        }
