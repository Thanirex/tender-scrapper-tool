import os
import re
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from date_utils import now_ist_naive


def _normalize(title: str) -> str:
    t = title.lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    return re.sub(r'\s+', ' ', t).strip()


# ── Database backend detection ─────────────────────────────────────────────────
#
# Set DATABASE_URL in .env to switch backends:
#   SQLite (default):   DATABASE_URL=sqlite:///path/to/custom.db
#   PostgreSQL:         DATABASE_URL=postgresql://user:pass@host:5432/dbname
#   MySQL:              DATABASE_URL=mysql://user:pass@host:3306/dbname
#                   or  DATABASE_URL=mysql+pymysql://user:pass@host:3306/dbname
#
# If DATABASE_URL is not set, SQLite at DB_PATH (from paths.py) is used.

_DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
_IS_PG    = _DATABASE_URL.startswith(("postgresql://", "postgres://"))
_IS_MYSQL = _DATABASE_URL.startswith(("mysql://", "mysql+pymysql://"))

if _IS_PG:
    _PK = "SERIAL PRIMARY KEY"
elif _IS_MYSQL:
    _PK = "INT AUTO_INCREMENT PRIMARY KEY"
else:
    _PK = "INTEGER PRIMARY KEY AUTOINCREMENT"

# MySQL requires VARCHAR for indexed columns; TEXT cannot be fully indexed without a prefix.
# title_norm is indexed for dedup; url is uniquely indexed in downloaded_tenders/cron_dedup.
# username/email carry inline UNIQUE constraints — MySQL needs a prefix length via VARCHAR.
_IDX_TEXT  = "VARCHAR(500)" if _IS_MYSQL else "TEXT"   # for indexed text columns
_URL_COL   = "VARCHAR(767)" if _IS_MYSQL else "TEXT"   # for uniquely-indexed url columns
_USER_TEXT = "VARCHAR(191)" if _IS_MYSQL else "TEXT"   # for UNIQUE username/email columns
# MySQL forbids DEFAULT values on TEXT/BLOB columns (error 1101); any column that
# carries a DEFAULT must be VARCHAR there.
_ENUM_TEXT = "VARCHAR(20)"  if _IS_MYSQL else "TEXT"   # short status/action columns with DEFAULTs
_KW_TEXT   = "VARCHAR(500)" if _IS_MYSQL else "TEXT"   # current_keyword (has DEFAULT '')


def _get_sqlite_path(fallback: str) -> str:
    """If DATABASE_URL is a sqlite:// URI, extract the file path from it."""
    if _DATABASE_URL.startswith("sqlite:///"):
        return _DATABASE_URL[len("sqlite:///"):]
    return fallback


def _insert_ignore(table: str, cols: str, ph: str) -> str:
    """Returns backend-appropriate insert-and-ignore-duplicates SQL."""
    if _IS_MYSQL:
        return f"INSERT IGNORE INTO {table} ({cols}) VALUES ({ph})"
    # SQLite and PostgreSQL both support ON CONFLICT DO NOTHING
    return f"INSERT INTO {table} ({cols}) VALUES ({ph}) ON CONFLICT DO NOTHING"


# ── Normalized cursor ──────────────────────────────────────────────────────────

class _Cursor:
    """Wraps sqlite3.Cursor, psycopg2 cursor, or PyMySQL DictCursor; always returns plain dicts."""

    def __init__(self, raw, conn_raw, pg: bool):
        self._c    = raw
        self._conn = conn_raw
        self._pg   = pg

    def fetchone(self) -> "dict | None":
        row = self._c.fetchone()
        if row is None:
            return None
        return row if isinstance(row, dict) else dict(row)

    def fetchall(self) -> "list[dict]":
        rows = self._c.fetchall()
        if not rows:
            return []
        return list(rows) if isinstance(rows[0], dict) else [dict(r) for r in rows]

    @property
    def lastrowid(self) -> "int | None":
        if self._pg:
            # lastval() returns the most recently generated SERIAL value in
            # the current session — safe to call immediately after an INSERT.
            try:
                tmp = self._conn.cursor()
                tmp.execute("SELECT lastval()")
                return tmp.fetchone()[0]
            except Exception:
                return None
        # Works for both SQLite (sqlite3.Cursor.lastrowid) and MySQL (PyMySQL cursor.lastrowid)
        return self._c.lastrowid

    @property
    def rowcount(self) -> int:
        return self._c.rowcount


# ── Normalized connection ──────────────────────────────────────────────────────

class _Conn:
    """Context-managed DB connection for SQLite, PostgreSQL, or MySQL.

    Usage is identical to sqlite3 connection context managers:

        with self._connect() as conn:
            conn.execute(sql, params)
            conn.commit()
    """

    def __init__(self, sqlite_path: str):
        self._path = sqlite_path
        self._raw  = None

    def __enter__(self) -> "_Conn":
        if _IS_PG:
            import psycopg2
            self._raw = psycopg2.connect(_DATABASE_URL)
        elif _IS_MYSQL:
            import pymysql
            import pymysql.cursors
            # Normalise mysql+pymysql:// → mysql:// so urlparse works correctly
            url = _DATABASE_URL.replace("mysql+pymysql://", "mysql://", 1)
            p   = urlparse(url)
            self._raw = pymysql.connect(
                host=p.hostname or "localhost",
                port=p.port or 3306,
                user=p.username or "",
                password=p.password or "",
                database=(p.path or "").lstrip("/"),
                charset="utf8mb4",
                cursorclass=pymysql.cursors.DictCursor,
                autocommit=False,
            )
        else:
            self._raw = sqlite3.connect(
                _get_sqlite_path(self._path), timeout=10, check_same_thread=False
            )
            self._raw.row_factory = sqlite3.Row
        return self

    def __exit__(self, exc_type, *_):
        if self._raw:
            try:
                if exc_type:
                    self._raw.rollback()
                else:
                    self._raw.commit()
            finally:
                self._raw.close()
                self._raw = None

    def execute(self, sql: str, params=()) -> _Cursor:
        """Execute SQL.  Rewrites ? → %s automatically for PostgreSQL and MySQL."""
        if _IS_PG:
            import psycopg2.extras
            sql = sql.replace("?", "%s")
            cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
            cur.execute(sql, params or ())
            return _Cursor(cur, self._raw, pg=True)
        if _IS_MYSQL:
            sql = sql.replace("?", "%s")
            cur = self._raw.cursor()
            cur.execute(sql, params or ())
            return _Cursor(cur, self._raw, pg=False)
        cur = self._raw.execute(sql, params or ())
        return _Cursor(cur, self._raw, pg=False)

    def commit(self):
        if self._raw:
            self._raw.commit()


# ── Main DB class ──────────────────────────────────────────────────────────────

class TenderDB:
    def __init__(self, db_path: Path):
        self._path = str(db_path)
        self._init_schema()

    def _connect(self) -> _Conn:
        return _Conn(self._path)

    def _add_col(self, conn: _Conn, table: str, col: str, defn: str):
        """Add a column to an existing table, silently skipping if it already exists."""
        if _IS_PG:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {defn}"
            )
        elif _IS_MYSQL:
            # MySQL has no ADD COLUMN IF NOT EXISTS; check INFORMATION_SCHEMA first.
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM INFORMATION_SCHEMA.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = ? AND COLUMN_NAME = ?",
                (table, col),
            ).fetchone()
            if not (row and row.get("cnt", 0) > 0):
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
        else:
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {defn}")
            except sqlite3.OperationalError:
                pass  # column already exists

    def _idx(self, conn: _Conn, unique: bool, name: str, table: str,
             col: str, prefix: int = 0):
        """Create an index, wrapping in try/except for already-exists errors.

        prefix is used for MySQL TEXT columns (e.g. run_date(10), timestamp(19)).
        It is ignored for non-MySQL backends and when col is already VARCHAR.
        """
        u        = "UNIQUE " if unique else ""
        col_expr = f"{col}({prefix})" if (_IS_MYSQL and prefix) else col
        try:
            conn.execute(f"CREATE {u}INDEX IF NOT EXISTS {name} ON {table} ({col_expr})")
        except Exception:
            pass

    def _url_idx(self, conn: _Conn, name: str, table: str):
        """Create the unique index on (url, team_id).
        SQLite and PostgreSQL use a partial index to allow multiple NULL / empty URLs.
        """
        try:
            try:
                conn.execute(f"DROP INDEX IF EXISTS {name}")
            except Exception:
                pass
            if _IS_MYSQL:
                conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {name} ON {table} (url, team_id)"
                )
            else:
                conn.execute(
                    f"CREATE UNIQUE INDEX IF NOT EXISTS {name} "
                    f"ON {table} (url, team_id) WHERE url IS NOT NULL AND url != ''"
                )
        except Exception:
            pass

    def _init_schema(self):
        with self._connect() as conn:
            # ── Dedup table ────────────────────────────────────────────────
            # title_norm and url are indexed; use VARCHAR widths for MySQL.
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS downloaded_tenders (
                    id              {_PK},
                    title_norm      {_IDX_TEXT} NOT NULL,
                    url             {_URL_COL},
                    site            TEXT,
                    keyword         TEXT,
                    published_date  TEXT,
                    downloaded_at   TEXT NOT NULL
                )
            """)
            self._idx(conn, False, "idx_title", "downloaded_tenders", "title_norm")
            self._url_idx(conn, "idx_url", "downloaded_tenders")

            # ── Users ──────────────────────────────────────────────────────
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS users (
                    id            {_PK},
                    username      {_USER_TEXT} NOT NULL UNIQUE,
                    email         {_USER_TEXT} NOT NULL UNIQUE,
                    password_hash TEXT    NOT NULL,
                    role          TEXT    NOT NULL
                                  CHECK(role IN ('superadmin','admin','user')),
                    created_by    INTEGER REFERENCES users(id),
                    created_at    TEXT    NOT NULL,
                    is_active     INTEGER NOT NULL DEFAULT 1
                )
            """)

            # ── Search sessions ────────────────────────────────────────────
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS search_sessions (
                    id           {_PK},
                    user_id      INTEGER NOT NULL REFERENCES users(id),
                    site         TEXT    NOT NULL,
                    run_date     TEXT    NOT NULL,
                    status       {_ENUM_TEXT} NOT NULL DEFAULT 'running',
                    zip_filename TEXT,
                    created_at   TEXT    NOT NULL
                )
            """)
            # run_date is always "YYYY-MM-DD" (10 chars); prefix(10) covers it in MySQL.
            self._idx(conn, False, "idx_sessions_date", "search_sessions", "run_date", prefix=10)

            # ── Keywords per session ───────────────────────────────────────
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS session_keywords (
                    id            {_PK},
                    session_id    INTEGER NOT NULL REFERENCES search_sessions(id),
                    keyword       TEXT    NOT NULL,
                    tenders_found INTEGER NOT NULL DEFAULT 0
                )
            """)

            # ── Found tenders (manual scrapes) ─────────────────────────────
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS found_tenders (
                    id             {_PK},
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
            self._add_col(conn, "found_tenders", "tender_dir", "TEXT")
            self._idx(conn, False, "idx_found_session", "found_tenders", "session_id")

            # ── Tender documents ───────────────────────────────────────────
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS tender_documents (
                    id         {_PK},
                    tender_id  INTEGER NOT NULL REFERENCES found_tenders(id),
                    filename   TEXT    NOT NULL,
                    file_path  TEXT    NOT NULL,
                    file_type  TEXT,
                    file_size  INTEGER,
                    stored_at  TEXT    NOT NULL
                )
            """)

            # ── Audit / activity log ───────────────────────────────────────
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS activity_logs (
                    id           {_PK},
                    user_id      INTEGER REFERENCES users(id),
                    username     TEXT,
                    action       TEXT    NOT NULL,
                    details_json TEXT,
                    ip_address   TEXT,
                    timestamp    TEXT    NOT NULL
                )
            """)
            # timestamp is always "YYYY-MM-DDTHH:MM:SS" (19 chars); prefix(19) covers it.
            self._idx(conn, False, "idx_logs_ts", "activity_logs", "timestamp", prefix=19)

            # ── TAiQ cron runs ─────────────────────────────────────────────
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS cron_runs (
                    id              {_PK},
                    run_date        TEXT    NOT NULL,
                    started_at      TEXT    NOT NULL,
                    finished_at     TEXT,
                    status          {_ENUM_TEXT} NOT NULL DEFAULT 'running',
                    total_keywords  INTEGER NOT NULL DEFAULT 0,
                    keywords_done   INTEGER NOT NULL DEFAULT 0,
                    total_tenders   INTEGER NOT NULL DEFAULT 0,
                    error_msg       TEXT,
                    stop_requested  INTEGER NOT NULL DEFAULT 0,
                    current_keyword {_KW_TEXT} NOT NULL DEFAULT '',
                    log_file        TEXT
                )
            """)
            self._add_col(conn, "cron_runs", "stop_requested",  "INTEGER NOT NULL DEFAULT 0")
            self._add_col(conn, "cron_runs", "current_keyword", f"{_KW_TEXT} NOT NULL DEFAULT ''")
            self._add_col(conn, "cron_runs", "log_file",        "TEXT")
            # Per-run report card (JSON from run_stats.RunStatsCollector)
            self._add_col(conn, "cron_runs", "stats_json",      "TEXT")
            self._idx(conn, False, "idx_cron_runs_date", "cron_runs", "run_date", prefix=10)

            # ── TAiQ cron tenders ──────────────────────────────────────────
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS cron_tenders (
                    id              {_PK},
                    run_id          INTEGER NOT NULL REFERENCES cron_runs(id),
                    keyword         TEXT    NOT NULL,
                    title           TEXT    NOT NULL,
                    url             TEXT,
                    site            TEXT    NOT NULL,
                    published_date  TEXT,
                    summary_json    TEXT,
                    tender_dir      TEXT,
                    found_at        TEXT    NOT NULL
                )
            """)
            self._idx(conn, False, "idx_cron_tenders_run", "cron_tenders", "run_id")

            # ── Cron dedup ─────────────────────────────────────────────────
            # title_norm and url are indexed; use VARCHAR widths for MySQL.
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS cron_dedup (
                    id              {_PK},
                    title_norm      {_IDX_TEXT} NOT NULL,
                    url             {_URL_COL},
                    site            TEXT,
                    keyword         TEXT,
                    published_date  TEXT,
                    found_at        TEXT    NOT NULL
                )
            """)
            self._idx(conn, False, "idx_cron_dedup_title", "cron_dedup", "title_norm")
            self._url_idx(conn, "idx_cron_dedup_url", "cron_dedup")

            # ── Teams table ────────────────────────────────────────────────
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS teams (
                    id          {_PK},
                    slug        {_ENUM_TEXT} NOT NULL UNIQUE,
                    name        TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    is_active   INTEGER NOT NULL DEFAULT 1
                )
            """)
            conn.execute(_insert_ignore("teams", "slug, name, created_at", "?, ?, ?"), ("cnk", "CNK", "2026-01-01"))
            conn.execute(_insert_ignore("teams", "slug, name, created_at", "?, ?, ?"), ("tmi", "TMI", "2026-01-01"))

            # ── Auto-migrate team_id column onto all primary tables ────────
            for _tbl in ("users", "search_sessions", "found_tenders", "cron_runs", "cron_tenders", "activity_logs", "downloaded_tenders", "cron_dedup"):
                self._add_col(conn, _tbl, "team_id", f"{_ENUM_TEXT} DEFAULT 'cnk'")
            self._add_col(conn, "users", "team_name", f"{_ENUM_TEXT} DEFAULT 'CNK'")

            # ── Tender review / feedback ───────────────────────────────────
            # review_status: 'pending' | 'approved' | 'rejected'
            # MySQL cannot put a DEFAULT on TEXT columns, so use VARCHAR there.
            _status_col = f"{_ENUM_TEXT} NOT NULL DEFAULT 'pending'"
            for _t in ("cron_tenders", "found_tenders"):
                self._add_col(conn, _t, "review_status",    _status_col)
                self._add_col(conn, _t, "reviewed_by",      "INTEGER")
                self._add_col(conn, _t, "reviewed_by_name", "TEXT")
                self._add_col(conn, _t, "reviewed_at",      "TEXT")

            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS tender_comments (
                    id         {_PK},
                    source     {_ENUM_TEXT} NOT NULL,
                    tender_id  INTEGER NOT NULL,
                    user_id    INTEGER,
                    username   TEXT,
                    action     {_ENUM_TEXT} NOT NULL DEFAULT 'comment',
                    comment    TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
            """)
            self._idx(conn, False, "idx_tender_comments_t", "tender_comments", "tender_id")

            # ── Retroactive repair: sync cron_tenders.team_id with parent cron_runs.team_id ──
            conn.execute("""
                UPDATE cron_tenders
                SET team_id = (SELECT team_id FROM cron_runs WHERE cron_runs.id = cron_tenders.run_id)
                WHERE run_id IN (SELECT id FROM cron_runs WHERE team_id != 'cnk')
                  AND (team_id IS NULL OR team_id = 'cnk')
            """)

    # ── Dedup ──────────────────────────────────────────────────────────────────

    def is_duplicate(self, title: str, url: str = "", team_id: str = "cnk") -> bool:
        with self._connect() as conn:
            tid = team_id or "cnk"
            if url:
                row = conn.execute(
                    "SELECT 1 FROM downloaded_tenders WHERE url = ? AND team_id = ? LIMIT 1", (url, tid)
                ).fetchone()
                if row:
                    return True
            norm = _normalize(title)
            row = conn.execute(
                "SELECT 1 FROM downloaded_tenders WHERE title_norm = ? AND team_id = ? LIMIT 1", (norm, tid)
            ).fetchone()
            return row is not None

    def mark_downloaded(self, title: str, url: str, site: str,
                        keyword: str, published_date: str = "", team_id: str = "cnk"):
        norm = _normalize(title)
        now  = now_ist_naive().isoformat(timespec="seconds")
        tid  = team_id or "cnk"
        with self._connect() as conn:
            conn.execute(
                _insert_ignore(
                    "downloaded_tenders",
                    "title_norm, url, site, keyword, published_date, downloaded_at, team_id",
                    "?, ?, ?, ?, ?, ?, ?",
                ),
                (norm, url or None, site or "", keyword or "", published_date or "", now, tid),
            )

    # ── User management ────────────────────────────────────────────────────────

    def create_user(self, username: str, email: str, password_hash: str,
                    role: str, created_by: int = None, team_id: str = "cnk", team_name: str = "CNK") -> int:
        now = now_ist_naive().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO users
                   (username, email, password_hash, role, created_by, created_at, team_id, team_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (username, email, password_hash, role, created_by, now, team_id or "cnk", team_name or "CNK"),
            )
            return cur.lastrowid

    def get_user_by_username(self, username: str) -> "dict | None":
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE username = ?", (username,)
            ).fetchone()

    def get_user_by_id(self, user_id: int) -> "dict | None":
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()

    def list_users(self, requester_role: str, requester_id: int) -> "list[dict]":
        with self._connect() as conn:
            if requester_role == "superadmin":
                return conn.execute(
                    """SELECT id, username, email, role, team_id, team_name, created_at, is_active, created_by
                       FROM users ORDER BY created_at DESC"""
                ).fetchall()
            return conn.execute(
                """SELECT id, username, email, role, team_id, team_name, created_at, is_active, created_by
                   FROM users
                   WHERE created_by = ? OR id = ?
                   ORDER BY created_at DESC""",
                (requester_id, requester_id),
            ).fetchall()

    def update_user(self, user_id: int, **kwargs):
        allowed = {"username", "email", "password_hash", "is_active"}
        fields  = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [user_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE users SET {sets} WHERE id = ?", vals)

    def superadmin_exists(self) -> bool:
        with self._connect() as conn:
            return bool(conn.execute(
                "SELECT 1 FROM users WHERE role='superadmin' LIMIT 1"
            ).fetchone())

    # ── Search sessions ────────────────────────────────────────────────────────

    def create_session(self, user_id: int, site: str, team_id: str = "cnk") -> int:
        now = now_ist_naive()
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO search_sessions
                   (user_id, site, run_date, status, created_at, team_id)
                   VALUES (?, ?, ?, 'running', ?, ?)""",
                (user_id, site, now.strftime("%Y-%m-%d"), now.isoformat(timespec="seconds"), team_id or "cnk"),
            )
            return cur.lastrowid

    def update_session_status(self, session_id: int, status: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE search_sessions SET status = ? WHERE id = ?",
                (status, session_id),
            )

    def update_session_zip(self, session_id: int, zip_filename: str):
        with self._connect() as conn:
            conn.execute(
                "UPDATE search_sessions SET zip_filename = ? WHERE id = ?",
                (zip_filename, session_id),
            )

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

    def record_found_tender(self, session_id: int, keyword: str, title: str,
                             url: str, site: str, published_date: str = "",
                             summary: dict = None, tender_dir: str = "", team_id: str = "cnk") -> int:
        now = now_ist_naive().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO found_tenders
                   (session_id, keyword, title, url, site, published_date,
                    summary_json, tender_dir, found_at, team_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (session_id, keyword, title, url or None, site,
                 published_date or "", json.dumps(summary or {}),
                 tender_dir or "", now, team_id or "cnk"),
            )
            return cur.lastrowid

    # ── Dashboard queries ──────────────────────────────────────────────────────

    def get_stats_for_date(self, date_str: str = None, start_date: str = None, end_date: str = None, team_id: str = None) -> dict:
        if start_date and end_date and start_date != end_date:
            return self.get_stats_for_date_range(start_date, end_date, team_id=team_id)
        if not date_str:
            date_str = start_date or now_ist_naive().strftime("%Y-%m-%d")

        with self._connect() as conn:
            sess_sql = """SELECT s.id, u.username, s.site, s.status, s.zip_filename,
                                s.created_at,
                                GROUP_CONCAT(DISTINCT sk.keyword) AS keywords,
                                COUNT(DISTINCT ft.id)             AS tenders_found
                         FROM search_sessions s
                         JOIN users u ON u.id = s.user_id
                         LEFT JOIN session_keywords sk ON sk.session_id = s.id
                         LEFT JOIN found_tenders ft ON ft.session_id = s.id
                         WHERE s.run_date = ?"""
            sess_params = [date_str]
            if team_id:
                sess_sql += " AND s.team_id = ?"
                sess_params.append(team_id)
            sess_sql += " GROUP BY s.id ORDER BY s.created_at"
            session_rows = conn.execute(sess_sql, sess_params).fetchall()

            site_sql = """SELECT s.site, COUNT(DISTINCT ft.id) AS count
                          FROM search_sessions s
                          LEFT JOIN found_tenders ft ON ft.session_id = s.id
                          WHERE s.run_date = ?"""
            site_params = [date_str]
            if team_id:
                site_sql += " AND s.team_id = ?"
                site_params.append(team_id)
            site_sql += " GROUP BY s.site"
            by_site_rows = conn.execute(site_sql, site_params).fetchall()

            cron_sql = "SELECT * FROM cron_runs WHERE run_date = ?"
            cron_params = [date_str]
            if team_id:
                cron_sql += " AND team_id = ?"
                cron_params.append(team_id)
            cron_sql += " ORDER BY id DESC"
            cron_rows = conn.execute(cron_sql, cron_params).fetchall()

            cron_ids = [c["id"] for c in cron_rows]
            ct_rows = []
            if cron_ids:
                ph = ",".join("?" for _ in cron_ids)
                ct_sql = f"SELECT LOWER(site) as site, COUNT(*) as count FROM cron_tenders WHERE run_id IN ({ph}) GROUP BY LOWER(site)"
                ct_rows = conn.execute(ct_sql, cron_ids).fetchall()

        sessions = list(session_rows)
        site_map = {}
        for r in by_site_rows:
            sn = r["site"].lower()
            site_map[sn] = site_map.get(sn, 0) + r["count"]

        for r in ct_rows:
            sn = r["site"]
            site_map[sn] = site_map.get(sn, 0) + r["count"]

        total_taiq = 0
        for cr in cron_rows:
            if cr["status"] in ("complete", "failed", "stopped"):
                t_count = cr["total_tenders"] or 0
                total_taiq += t_count
                sessions.append({
                    "id":            f"taiq_{cr['id']}",
                    "username":      "TAiQ",
                    "site":          "taiq",
                    "status":        cr["status"],
                    "zip_filename":  None,
                    "created_at":    cr["started_at"],
                    "keywords":      f"{cr['keywords_done']}/{cr['total_keywords']} keywords",
                    "tenders_found": t_count,
                    "source":        "taiq",
                })
                if s_json := cr.get("stats_json"):
                    try:
                        s_data = json.loads(s_json)
                        for sn, sd in s_data.get("sites", {}).items():
                            sn_l = sn.lower()
                            saved_cnt = sd.get("saved", 0)
                            if saved_cnt > 0 and sn_l not in site_map:
                                site_map[sn_l] = saved_cnt
                    except Exception:
                        pass

        by_site = [{"site": k, "count": v} for k, v in site_map.items()]
        if total_taiq > 0:
            by_site.append({"site": "taiq", "count": total_taiq})

        return {"sessions": sessions, "by_site": by_site}

    def get_stats_for_date_range(self, start_date: str, end_date: str, team_id: str = None) -> dict:
        if start_date > end_date:
            start_date, end_date = end_date, start_date

        with self._connect() as conn:
            sess_sql = """SELECT s.id, u.username, s.site, s.status, s.zip_filename,
                                s.created_at,
                                GROUP_CONCAT(DISTINCT sk.keyword) AS keywords,
                                COUNT(DISTINCT ft.id)             AS tenders_found
                         FROM search_sessions s
                         JOIN users u ON u.id = s.user_id
                         LEFT JOIN session_keywords sk ON sk.session_id = s.id
                         LEFT JOIN found_tenders ft ON ft.session_id = s.id
                         WHERE s.run_date BETWEEN ? AND ?"""
            sess_params = [start_date, end_date]
            if team_id:
                sess_sql += " AND s.team_id = ?"
                sess_params.append(team_id)
            sess_sql += " GROUP BY s.id ORDER BY s.created_at"
            session_rows = conn.execute(sess_sql, sess_params).fetchall()

            site_sql = """SELECT s.site, COUNT(DISTINCT ft.id) AS count
                          FROM search_sessions s
                          LEFT JOIN found_tenders ft ON ft.session_id = s.id
                          WHERE s.run_date BETWEEN ? AND ?"""
            site_params = [start_date, end_date]
            if team_id:
                site_sql += " AND s.team_id = ?"
                site_params.append(team_id)
            site_sql += " GROUP BY s.site"
            by_site_rows = conn.execute(site_sql, site_params).fetchall()

            cron_sql = "SELECT * FROM cron_runs WHERE run_date BETWEEN ? AND ?"
            cron_params = [start_date, end_date]
            if team_id:
                cron_sql += " AND team_id = ?"
                cron_params.append(team_id)
            cron_sql += " ORDER BY id DESC"
            cron_rows = conn.execute(cron_sql, cron_params).fetchall()

            cron_ids = [c["id"] for c in cron_rows]
            ct_rows = []
            if cron_ids:
                ph = ",".join("?" for _ in cron_ids)
                ct_sql = f"SELECT LOWER(site) as site, COUNT(*) as count FROM cron_tenders WHERE run_id IN ({ph}) GROUP BY LOWER(site)"
                ct_rows = conn.execute(ct_sql, cron_ids).fetchall()

        sessions = list(session_rows)
        site_map = {}
        for r in by_site_rows:
            sn = r["site"].lower()
            site_map[sn] = site_map.get(sn, 0) + r["count"]

        for r in ct_rows:
            sn = r["site"]
            site_map[sn] = site_map.get(sn, 0) + r["count"]

        total_taiq = 0
        for cr in cron_rows:
            if cr["status"] in ("complete", "failed", "stopped"):
                t_count = cr["total_tenders"] or 0
                total_taiq += t_count
                sessions.append({
                    "id":            f"taiq_{cr['id']}",
                    "username":      "TAiQ",
                    "site":          "taiq",
                    "status":        cr["status"],
                    "zip_filename":  None,
                    "created_at":    cr["started_at"],
                    "keywords":      f"{cr['keywords_done']}/{cr['total_keywords']} keywords",
                    "tenders_found": t_count,
                    "source":        "taiq",
                })
                if s_json := cr.get("stats_json"):
                    try:
                        s_data = json.loads(s_json)
                        for sn, sd in s_data.get("sites", {}).items():
                            sn_l = sn.lower()
                            saved_cnt = sd.get("saved", 0)
                            if saved_cnt > 0 and sn_l not in site_map:
                                site_map[sn_l] = saved_cnt
                    except Exception:
                        pass

        by_site = [{"site": k, "count": v} for k, v in site_map.items()]
        if total_taiq > 0:
            by_site.append({"site": "taiq", "count": total_taiq})

        return {"sessions": sessions, "by_site": by_site}

    def get_tenders_for_date(self, date_str: str,
                              site: str = None, keyword: str = None, team_id: str = None) -> "list[dict]":
        result = []

        # 1. Fetch manual search tenders from found_tenders
        if site != "taiq":
            with self._connect() as conn:
                q = """SELECT ft.*
                       FROM found_tenders ft
                       JOIN search_sessions s ON s.id = ft.session_id
                       WHERE s.run_date = ?"""
                params: list = [date_str]
                if team_id:
                    q += " AND s.team_id = ?"
                    params.append(team_id)
                if site and site != "taiq":
                    q += " AND LOWER(s.site) = LOWER(?)"
                    params.append(site)
                if keyword:
                    q += " AND ft.keyword LIKE ?"
                    params.append(f"%{keyword}%")
                q += " ORDER BY ft.found_at DESC"
                rows = conn.execute(q, params).fetchall()
            for r in rows:
                try:
                    r["fields"] = json.loads(r.get("summary_json") or "{}")
                except Exception:
                    r["fields"] = {}
                r["source"] = "manual"
                result.append(r)

        # 2. Fetch TAiQ cron tenders from cron_tenders
        with self._connect() as conn:
            cron_runs_q = "SELECT id FROM cron_runs WHERE run_date = ?"
            cron_params = [date_str]
            if team_id:
                cron_runs_q += " AND team_id = ?"
                cron_params.append(team_id)
            run_rows = conn.execute(cron_runs_q, cron_params).fetchall()
            run_ids = [r["id"] for r in run_rows]

            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                q = f"SELECT * FROM cron_tenders WHERE run_id IN ({placeholders})"
                params = list(run_ids)
                if team_id:
                    q += " AND team_id = ?"
                    params.append(team_id)
                if site and site != "taiq":
                    q += " AND LOWER(site) = LOWER(?)"
                    params.append(site)
                if keyword:
                    q += " AND keyword LIKE ?"
                    params.append(f"%{keyword}%")
                q += " ORDER BY found_at DESC"
                rows = conn.execute(q, params).fetchall()
                for r in rows:
                    try:
                        r["fields"] = json.loads(r.get("summary_json") or "{}")
                    except Exception:
                        r["fields"] = {}
                    r["source"] = "taiq"
                    result.append(r)

        result.sort(key=lambda x: x.get("found_at", ""), reverse=True)
        return result

    def get_tenders_for_date_range(self, start_date: str, end_date: str = None,
                                    site: str = None, keyword: str = None, team_id: str = None) -> "list[dict]":
        if not end_date or end_date == start_date:
            return self.get_tenders_for_date(start_date, site=site, keyword=keyword, team_id=team_id)

        result = []
        if start_date > end_date:
            start_date, end_date = end_date, start_date

        # 1. Fetch manual search tenders from found_tenders
        if site != "taiq":
            with self._connect() as conn:
                q = """SELECT ft.*
                       FROM found_tenders ft
                       JOIN search_sessions s ON s.id = ft.session_id
                       WHERE s.run_date BETWEEN ? AND ?"""
                params: list = [start_date, end_date]
                if team_id:
                    q += " AND s.team_id = ?"
                    params.append(team_id)
                if site and site != "taiq":
                    q += " AND LOWER(s.site) = LOWER(?)"
                    params.append(site)
                if keyword:
                    q += " AND ft.keyword LIKE ?"
                    params.append(f"%{keyword}%")
                q += " ORDER BY ft.found_at DESC"
                rows = conn.execute(q, params).fetchall()
            for r in rows:
                try:
                    r["fields"] = json.loads(r.get("summary_json") or "{}")
                except Exception:
                    r["fields"] = {}
                r["source"] = "manual"
                result.append(r)

        # 2. Fetch TAiQ cron tenders from cron_tenders
        with self._connect() as conn:
            cron_runs_q = "SELECT id FROM cron_runs WHERE run_date BETWEEN ? AND ?"
            cron_params = [start_date, end_date]
            if team_id:
                cron_runs_q += " AND team_id = ?"
                cron_params.append(team_id)
            run_rows = conn.execute(cron_runs_q, cron_params).fetchall()
            run_ids = [r["id"] for r in run_rows]

            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                q = f"SELECT * FROM cron_tenders WHERE run_id IN ({placeholders})"
                params = list(run_ids)
                if team_id:
                    q += " AND team_id = ?"
                    params.append(team_id)
                if site and site != "taiq":
                    q += " AND LOWER(site) = LOWER(?)"
                    params.append(site)
                if keyword:
                    q += " AND keyword LIKE ?"
                    params.append(f"%{keyword}%")
                q += " ORDER BY found_at DESC"
                rows = conn.execute(q, params).fetchall()
                for r in rows:
                    try:
                        r["fields"] = json.loads(r.get("summary_json") or "{}")
                    except Exception:
                        r["fields"] = {}
                    r["source"] = "taiq"
                    result.append(r)

        result.sort(key=lambda x: x.get("found_at", ""), reverse=True)
        return result

    def get_dates_with_data(self, team_id: str = None) -> "list[str]":
        with self._connect() as conn:
            m_sql = "SELECT DISTINCT run_date FROM search_sessions WHERE status = 'complete'"
            m_params = []
            if team_id:
                m_sql += " AND team_id = ?"
                m_params.append(team_id)
            m_sql += " ORDER BY run_date DESC LIMIT 90"

            c_sql = "SELECT DISTINCT run_date FROM cron_runs WHERE status IN ('complete', 'stopped', 'failed')"
            c_params = []
            if team_id:
                c_sql += " AND team_id = ?"
                c_params.append(team_id)
            c_sql += " ORDER BY run_date DESC LIMIT 90"

            manual_rows = conn.execute(m_sql, m_params).fetchall()
            cron_rows   = conn.execute(c_sql, c_params).fetchall()
        manual_dates = {r["run_date"] for r in manual_rows}
        cron_dates   = {r["run_date"] for r in cron_rows}
        return sorted(manual_dates | cron_dates, reverse=True)[:90]

    def get_daily_report(self, days: int = 3, team_id: str = None) -> "list[dict]":
        """Report cards for the last `days` IST days, newest first.

        Per day: tenders scraped (TAiQ vs manual split), site coverage
        (scanned vs produced results) and review progress for the tenders
        found that day.
        """
        from datetime import timedelta
        report: list = []
        today = now_ist_naive().date()

        with self._connect() as conn:
            for i in range(max(1, days)):
                d    = (today - timedelta(days=i)).strftime("%Y-%m-%d")
                like = f"{d}%"

                # ── TAiQ run of the day ────────────────────────────────────
                c_sql = "SELECT * FROM cron_runs WHERE run_date = ?"
                c_params = [d]
                if team_id:
                    c_sql += " AND team_id = ?"
                    c_params.append(team_id)
                c_sql += " ORDER BY id DESC LIMIT 1"
                cron_row = conn.execute(c_sql, c_params).fetchone()

                taiq_count    = (cron_row["total_tenders"] or 0) if cron_row else 0
                scanned: set  = set()
                produced: set = set()
                if cron_row:
                    try:
                        stats = json.loads(cron_row.get("stats_json") or "{}")
                        scanned |= {s.lower() for s in stats.get("sites", {})}
                    except Exception:
                        pass
                    rows = conn.execute(
                        "SELECT DISTINCT site FROM cron_tenders WHERE run_id = ?",
                        (cron_row["id"],),
                    ).fetchall()
                    produced |= {(r["site"] or "").lower() for r in rows if r["site"]}

                # ── Manual sessions of the day ─────────────────────────────
                m_sql = """SELECT COUNT(ft.id) AS n
                           FROM found_tenders ft
                           JOIN search_sessions s ON s.id = ft.session_id
                           WHERE s.run_date = ?"""
                m_params = [d]
                if team_id:
                    m_sql += " AND s.team_id = ?"
                    m_params.append(team_id)
                manual_count = conn.execute(m_sql, m_params).fetchone()["n"]

                ms_sql = """SELECT s.site, COUNT(ft.id) AS n
                            FROM search_sessions s
                            LEFT JOIN found_tenders ft ON ft.session_id = s.id
                            WHERE s.run_date = ?"""
                ms_params = [d]
                if team_id:
                    ms_sql += " AND s.team_id = ?"
                    ms_params.append(team_id)
                ms_sql += " GROUP BY s.site"

                for r in conn.execute(ms_sql, ms_params).fetchall():
                    site = (r["site"] or "").lower()
                    if site:
                        scanned.add(site)
                        if r["n"]:
                            produced.add(site)
                scanned |= produced

                # ── Review progress for tenders found that day ─────────────
                counts = {"approved": 0, "rejected": 0, "pending": 0}
                for table in self._REVIEW_SOURCES.values():
                    rev_sql = f"""SELECT COALESCE(review_status, 'pending') AS st,
                                        COUNT(*) AS n
                                 FROM {table} WHERE found_at LIKE ?"""
                    rev_params = [like]
                    if team_id:
                        rev_sql += " AND team_id = ?"
                        rev_params.append(team_id)
                    rev_sql += " GROUP BY COALESCE(review_status, 'pending')"
                    for r in conn.execute(rev_sql, rev_params).fetchall():
                        counts[r["st"]] = counts.get(r["st"], 0) + r["n"]
                decided = counts["approved"] + counts["rejected"]

                report.append({
                    "date":            d,
                    "scraped":         taiq_count + manual_count,
                    "taiq":            taiq_count,
                    "manual":          manual_count,
                    "taiq_status":     cron_row["status"] if cron_row else None,
                    "sites_scanned":   len(scanned),
                    "sites_with_results": len(produced),
                    "approved":        counts["approved"],
                    "rejected":        counts["rejected"],
                    "pending":         counts["pending"],
                    "approval_rate":   (round(100 * counts["approved"] / decided)
                                        if decided else None),
                })
        return report

    # ── Activity logging ───────────────────────────────────────────────────────

    def log_activity(self, user_id: int, username: str, action: str,
                     details: dict = None, ip_address: str = None):
        now = now_ist_naive().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO activity_logs
                   (user_id, username, action, details_json, ip_address, timestamp)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (user_id, username, action,
                 json.dumps(details or {}), ip_address, now),
            )

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
                f"SELECT COUNT(*) AS n {base_q}", params
            ).fetchone()["n"]
            rows = conn.execute(
                f"SELECT * {base_q} ORDER BY timestamp DESC LIMIT ? OFFSET ?",
                params + [limit, offset],
            ).fetchall()

        return {"total": total, "page": page, "limit": limit, "items": rows}

    # ── TAiQ cron ──────────────────────────────────────────────────────────────

    def create_cron_run(self, total_keywords: int = 0, team_id: str = "cnk") -> int:
        now = now_ist_naive().isoformat(timespec="seconds")
        run_date = now_ist_naive().strftime("%Y-%m-%d")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO cron_runs
                   (run_date, started_at, status, total_keywords, team_id)
                   VALUES (?, ?, 'running', ?, ?)""",
                (run_date, now, total_keywords, team_id or "cnk"),
            )
            return cur.lastrowid

    def update_cron_run(self, run_id: int, **kwargs):
        allowed = {"status", "finished_at", "keywords_done", "total_tenders",
                   "error_msg", "total_keywords", "current_keyword",
                   "stop_requested", "log_file", "stats_json"}
        fields = {k: v for k, v in kwargs.items() if k in allowed}
        if not fields:
            return
        sets = ", ".join(f"{k} = ?" for k in fields)
        vals = list(fields.values()) + [run_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE cron_runs SET {sets} WHERE id = ?", vals)

    def record_cron_tender(self, run_id: int, keyword: str, title: str,
                            url: str, site: str, published_date: str = "",
                            summary: dict = None, tender_dir: str = "", team_id: str = "cnk") -> int:
        now = now_ist_naive().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """INSERT INTO cron_tenders
                   (run_id, keyword, title, url, site, published_date,
                    summary_json, tender_dir, found_at, team_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (run_id, keyword, title, url or None, site,
                 published_date or "", json.dumps(summary or {}),
                 tender_dir or "", now, team_id or "cnk"),
            )
            return cur.lastrowid

    def is_cron_duplicate(self, title: str, url: str = "", team_id: str = "cnk") -> bool:
        with self._connect() as conn:
            tid = team_id or "cnk"
            if url:
                row = conn.execute(
                    "SELECT 1 FROM cron_dedup WHERE url = ? AND team_id = ? LIMIT 1", (url, tid)
                ).fetchone()
                if row:
                    return True
            norm = _normalize(title)
            return bool(conn.execute(
                "SELECT 1 FROM cron_dedup WHERE title_norm = ? AND team_id = ? LIMIT 1", (norm, tid)
            ).fetchone())

    def mark_cron_seen(self, title: str, url: str, site: str,
                       keyword: str, published_date: str = "", team_id: str = "cnk"):
        norm = _normalize(title)
        now  = now_ist_naive().isoformat(timespec="seconds")
        tid  = team_id or "cnk"
        with self._connect() as conn:
            conn.execute(
                _insert_ignore(
                    "cron_dedup",
                    "title_norm, url, site, keyword, published_date, found_at, team_id",
                    "?, ?, ?, ?, ?, ?, ?",
                ),
                (norm, url or None, site or "", keyword or "", published_date or "", now, tid),
            )

    def get_cron_run(self, run_id: int) -> "dict | None":
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM cron_runs WHERE id = ?", (run_id,)
            ).fetchone()

    def get_latest_cron_run(self, team_id: str = None) -> "dict | None":
        with self._connect() as conn:
            q = "SELECT * FROM cron_runs"
            params = []
            if team_id:
                q += " WHERE team_id = ?"
                params.append(team_id)
            q += " ORDER BY id DESC LIMIT 1"
            return conn.execute(q, params).fetchone()

    def get_cron_tenders(self, run_id: int,
                          site: str = None, keyword: str = None, team_id: str = None) -> "list[dict]":
        with self._connect() as conn:
            q      = "SELECT * FROM cron_tenders WHERE run_id = ?"
            params: list = [run_id]
            if team_id:
                q += " AND team_id = ?"
                params.append(team_id)
            if site:
                q += " AND site = ?"
                params.append(site)
            if keyword:
                q += " AND keyword LIKE ?"
                params.append(f"%{keyword}%")
            q += " ORDER BY found_at DESC"
            rows = conn.execute(q, params).fetchall()
        result = []
        for r in rows:
            try:
                r["fields"] = json.loads(r.get("summary_json") or "{}")
            except Exception:
                r["fields"] = {}
            result.append(r)
        return result

    def get_cron_dates(self, team_id: str = None) -> "list[dict]":
        with self._connect() as conn:
            q = "SELECT run_date, status FROM cron_runs"
            params = []
            if team_id:
                q += " WHERE team_id = ?"
                params.append(team_id)
            q += " ORDER BY run_date DESC LIMIT 90"
            rows = conn.execute(q, params).fetchall()
        seen: dict = {}
        for r in rows:
            d, s = r["run_date"], r["status"]
            if d not in seen or s == "running":
                seen[d] = s
        return [{"date": d, "status": s} for d, s in seen.items()]

    def get_cron_run_by_date(self, date_str: str, team_id: str = None) -> "dict | None":
        with self._connect() as conn:
            q = "SELECT * FROM cron_runs WHERE run_date = ?"
            params = [date_str]
            if team_id:
                q += " AND team_id = ?"
                params.append(team_id)
            q += " ORDER BY id DESC LIMIT 1"
            return conn.execute(q, params).fetchone()

    def get_taiq_detailed_report(self, date_str: str = None, start_date: str = None, end_date: str = None, team_id: str = "cnk") -> dict:
        tid = team_id or "cnk"
        if start_date and end_date and start_date != end_date:
            if start_date > end_date:
                start_date, end_date = end_date, start_date
            with self._connect() as conn:
                cron_runs = conn.execute(
                    "SELECT * FROM cron_runs WHERE run_date BETWEEN ? AND ? AND team_id = ? ORDER BY id DESC",
                    (start_date, end_date, tid),
                ).fetchall()
            if not cron_runs:
                return {"run": None, "report": None}

            combined_totals = {
                "sites_scanned": 0,
                "listed": 0,
                "saved": 0,
                "rejected_total": 0,
                "rejected": {}
            }
            sites_map = {}
            run_ids = [r["id"] for r in cron_runs]

            for r in cron_runs:
                combined_totals["saved"] += (r.get("total_tenders", 0) or 0)
                if s_json := r.get("stats_json"):
                    try:
                        s_data = json.loads(s_json)
                        tot = s_data.get("totals", {})
                        combined_totals["sites_scanned"] = max(combined_totals["sites_scanned"], tot.get("sites_scanned", 0))
                        combined_totals["listed"] += tot.get("listed", 0)
                        combined_totals["rejected_total"] += tot.get("rejected_total", 0)
                        for rk, rv in tot.get("rejected", {}).items():
                            combined_totals["rejected"][rk] = combined_totals["rejected"].get(rk, 0) + rv

                        for sn, sd in s_data.get("sites", {}).items():
                            sn_u = sn.upper()
                            if sn_u not in sites_map:
                                sites_map[sn_u] = {"site": sn_u, "listed": 0, "saved": 0, "rejected": 0, "error_cnt": 0}
                            sites_map[sn_u]["listed"] += sd.get("listed", 0)
                            sites_map[sn_u]["saved"] += sd.get("saved", 0)
                            sites_map[sn_u]["rejected"] += sd.get("rejected_total", 0)
                            sites_map[sn_u]["error_cnt"] += sd.get("rejected", {}).get("error", 0)
                    except Exception:
                        pass

            sites_list = []
            for sn_u, s_data in sites_map.items():
                listed = s_data["listed"]
                saved = s_data["saved"]
                rejected = s_data["rejected"]
                yield_pct = round((saved / listed * 100), 2) if listed > 0 else 0.0
                if s_data["error_cnt"] > 0 and saved == 0:
                    status = "warning"
                elif saved > 0:
                    status = "healthy"
                elif listed > 0:
                    status = "low_yield"
                else:
                    status = "idle"
                sites_list.append({
                    "site": sn_u,
                    "listed": listed,
                    "saved": saved,
                    "rejected": rejected,
                    "yield_pct": yield_pct,
                    "status": status
                })

            with self._connect() as conn:
                placeholders = ",".join("?" for _ in run_ids)
                kw_rows = conn.execute(
                    f"""SELECT keyword, COUNT(*) as count 
                       FROM cron_tenders 
                       WHERE run_id IN ({placeholders}) AND team_id = ?
                       GROUP BY keyword 
                       ORDER BY count DESC LIMIT 10""",
                    (*run_ids, tid)
                ).fetchall()
            top_keywords = [{"keyword": r["keyword"], "count": r["count"]} for r in kw_rows]

            latest_run = dict(cron_runs[0])
            latest_run["total_tenders"] = combined_totals["saved"]

            return {
                "run": latest_run,
                "totals": combined_totals,
                "sites": sites_list,
                "top_keywords": top_keywords,
                "trend": {"avg_7d": 0, "diff": 0, "pct_change": 0}
            }

        if not date_str:
            date_str = start_date or now_ist_naive().strftime("%Y-%m-%d")

        cron_run = self.get_cron_run_by_date(date_str, team_id=tid)
        if not cron_run:
            cron_run = self.get_latest_cron_run(team_id=tid)

        if not cron_run:
            return {"run": None, "report": None}

        run_id = cron_run["id"]
        stats_data = {}
        if cron_row_json := cron_run.get("stats_json"):
            try:
                stats_data = json.loads(cron_row_json)
            except Exception:
                pass

        totals = stats_data.get("totals", {
            "sites_scanned": 0,
            "listed": 0,
            "saved": cron_run.get("total_tenders", 0),
            "rejected_total": 0,
            "rejected": {}
        })
        sites_dict = stats_data.get("sites", {})

        sites_list = []
        for s_name, s_data in sites_dict.items():
            listed = s_data.get("listed", 0)
            saved = s_data.get("saved", 0)
            rejected = s_data.get("rejected_total", 0)
            yield_pct = round((saved / listed * 100), 2) if listed > 0 else 0.0
            
            if s_data.get("rejected", {}).get("error", 0) > 0 and saved == 0:
                status = "warning"
            elif saved > 0:
                status = "healthy"
            elif listed > 0:
                status = "low_yield"
            else:
                status = "idle"

            sites_list.append({
                "site": s_name.upper(),
                "listed": listed,
                "saved": saved,
                "rejected": rejected,
                "yield_pct": yield_pct,
                "status": status
            })

        with self._connect() as conn:
            kw_rows = conn.execute(
                """SELECT keyword, COUNT(*) as count 
                   FROM cron_tenders 
                   WHERE run_id = ? AND team_id = ?
                   GROUP BY keyword 
                   ORDER BY count DESC LIMIT 10""",
                (run_id, tid)
            ).fetchall()
        top_keywords = [{"keyword": r["keyword"], "count": r["count"]} for r in kw_rows]

        with self._connect() as conn:
            avg_row = conn.execute(
                """SELECT AVG(total_tenders) as avg_tenders 
                   FROM cron_runs 
                   WHERE team_id = ? AND status = 'complete' AND id != ?
                   ORDER BY id DESC LIMIT 7""",
                (tid, run_id)
            ).fetchone()
        avg_tenders = round(avg_row["avg_tenders"], 1) if avg_row and avg_row["avg_tenders"] else 0.0
        saved_today = cron_run.get("total_tenders", 0) or 0
        diff = saved_today - avg_tenders
        pct_change = round((diff / avg_tenders * 100), 1) if avg_tenders > 0 else 0.0

        return {
            "run": cron_run,
            "totals": totals,
            "sites": sites_list,
            "top_keywords": top_keywords,
            "trend": {
                "avg_7d": avg_tenders,
                "diff": diff,
                "pct_change": pct_change
            }
        }

    def mark_stale_runs_failed(self) -> int:
        now = now_ist_naive().isoformat(timespec="seconds")
        with self._connect() as conn:
            cur = conn.execute(
                """UPDATE cron_runs
                   SET status='failed', finished_at=?, error_msg='Server restarted mid-run'
                   WHERE status='running'""",
                (now,),
            )
            return cur.rowcount

    def request_cron_stop(self, run_id: int):
        with self._connect() as conn:
            conn.execute(
                "UPDATE cron_runs SET stop_requested = 1 WHERE id = ?", (run_id,)
            )

    # ── Tender review / feedback ───────────────────────────────────────────────
    #
    # Review state lives on the tender row itself (cron_tenders / found_tenders).
    # Every decision or comment also appends a row to tender_comments, so the
    # full "why" history survives edits and is visible to everyone.

    _REVIEW_SOURCES = {"taiq": "cron_tenders", "manual": "found_tenders"}

    def _review_table(self, source: str) -> str:
        table = self._REVIEW_SOURCES.get(source)
        if not table:
            raise ValueError(f"Unknown review source: {source!r}")
        return table

    @staticmethod
    def _attach_fields(row: dict) -> dict:
        try:
            row["fields"] = json.loads(row.get("summary_json") or "{}")
        except Exception:
            row["fields"] = {}
        row["review_status"] = row.get("review_status") or "pending"
        return row

    def get_review_tender(self, source: str, tender_id: int) -> "dict | None":
        table = self._review_table(source)
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (tender_id,)
            ).fetchone()
        if row:
            self._attach_fields(row)
            row["source"] = source
        return row

    def set_tender_review(self, source: str, tender_id: int, status: str,
                          user_id: int, username: str, comment: str):
        table = self._review_table(source)
        now   = now_ist_naive().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                f"""UPDATE {table}
                    SET review_status = ?, reviewed_by = ?,
                        reviewed_by_name = ?, reviewed_at = ?
                    WHERE id = ?""",
                (status, user_id, username, now, tender_id),
            )
            conn.execute(
                """INSERT INTO tender_comments
                   (source, tender_id, user_id, username, action, comment, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (source, tender_id, user_id, username, status, comment, now),
            )

    def add_tender_comment(self, source: str, tender_id: int,
                           user_id: int, username: str, comment: str):
        self._review_table(source)   # validates source
        now = now_ist_naive().isoformat(timespec="seconds")
        with self._connect() as conn:
            conn.execute(
                """INSERT INTO tender_comments
                   (source, tender_id, user_id, username, action, comment, created_at)
                   VALUES (?, ?, ?, ?, 'comment', ?, ?)""",
                (source, tender_id, user_id, username, comment, now),
            )

    def get_tender_comments(self, source: str, tender_id: int) -> "list[dict]":
        with self._connect() as conn:
            return conn.execute(
                """SELECT * FROM tender_comments
                   WHERE source = ? AND tender_id = ?
                   ORDER BY created_at ASC, id ASC""",
                (source, tender_id),
            ).fetchall()

    def get_review_summary(self, month: str, team_id: str = None) -> dict:
        """Counts + review metrics for one IST month ('YYYY-MM')."""
        like   = f"{month}%"
        counts = {"approved": 0, "rejected": 0, "pending": 0}
        review_spans: list = []   # (found_at, reviewed_at) of decided tenders
        with self._connect() as conn:
            for table in self._REVIEW_SOURCES.values():
                q = f"""SELECT COALESCE(review_status, 'pending') AS st,
                               COUNT(*) AS n
                        FROM {table} WHERE found_at LIKE ?"""
                params = [like]
                if team_id:
                    q += " AND team_id = ?"
                    params.append(team_id)
                q += " GROUP BY COALESCE(review_status, 'pending')"
                rows = conn.execute(q, params).fetchall()
                for r in rows:
                    counts[r["st"]] = counts.get(r["st"], 0) + r["n"]

                sq = f"""SELECT found_at, reviewed_at FROM {table}
                         WHERE found_at LIKE ?
                           AND COALESCE(review_status, 'pending') != 'pending'
                           AND reviewed_at IS NOT NULL"""
                sparams = [like]
                if team_id:
                    sq += " AND team_id = ?"
                    sparams.append(team_id)
                review_spans += conn.execute(sq, sparams).fetchall()

        counts["scraped"] = counts["approved"] + counts["rejected"] + counts["pending"]

        # Approval rate = share of decided tenders that were approved
        decided = counts["approved"] + counts["rejected"]
        counts["decided"]       = decided
        counts["approval_rate"] = round(100 * counts["approved"] / decided) if decided else None

        # Average time from scrape to decision, in hours
        hours: list = []
        for r in review_spans:
            try:
                delta = (datetime.fromisoformat(r["reviewed_at"])
                         - datetime.fromisoformat(r["found_at"])).total_seconds() / 3600
                if delta >= 0:
                    hours.append(delta)
            except (ValueError, TypeError):
                continue
        counts["avg_review_hours"] = round(sum(hours) / len(hours), 1) if hours else None
        return counts

    def get_review_months(self, limit: int = 6, team_id: str = None) -> "list[dict]":
        """Per-month counts for the trend chart, oldest → newest."""
        buckets: dict = {}
        with self._connect() as conn:
            for table in self._REVIEW_SOURCES.values():
                q = f"""SELECT SUBSTR(found_at, 1, 7) AS m,
                               COALESCE(review_status, 'pending') AS st,
                               COUNT(*) AS n
                        FROM {table}"""
                params = []
                if team_id:
                    q += " WHERE team_id = ?"
                    params.append(team_id)
                q += " GROUP BY SUBSTR(found_at, 1, 7), COALESCE(review_status, 'pending')"
                rows = conn.execute(q, params).fetchall()
                for r in rows:
                    b = buckets.setdefault(
                        r["m"], {"month": r["m"], "approved": 0,
                                 "rejected": 0, "pending": 0}
                    )
                    b[r["st"]] = b.get(r["st"], 0) + r["n"]
        months = sorted(buckets.values(), key=lambda b: b["month"], reverse=True)[:limit]
        for b in months:
            b["scraped"] = b["approved"] + b["rejected"] + b["pending"]
        return list(reversed(months))

    def get_review_tenders(self, month: str, status: str = None,
                           q: str = None, team_id: str = None) -> "list[dict]":
        like   = f"{month}%"
        result = []
        with self._connect() as conn:
            for source, table in self._REVIEW_SOURCES.items():
                sql    = f"SELECT * FROM {table} WHERE found_at LIKE ?"
                params: list = [like]
                if team_id:
                    sql += " AND team_id = ?"
                    params.append(team_id)
                if status:
                    sql += " AND COALESCE(review_status, 'pending') = ?"
                    params.append(status)
                if q:
                    sql += " AND (title LIKE ? OR keyword LIKE ? OR site LIKE ?)"
                    params += [f"%{q}%"] * 3
                sql += " ORDER BY found_at DESC"
                for r in conn.execute(sql, params).fetchall():
                    self._attach_fields(r)
                    r.pop("summary_json", None)
                    r["source"] = source
                    result.append(r)
        result.sort(key=lambda x: x.get("found_at", ""), reverse=True)
        return result


# ── CronDBProxy ────────────────────────────────────────────────────────────────

class CronDBProxy:
    """Wraps TenderDB so scraper agents use cron-specific dedup tables."""

    def __init__(self, db: TenderDB, run_id: int, team_id: str = "cnk"):
        self._db     = db
        self.run_id  = run_id
        self.team_id = team_id or "cnk"

    # NOTE: agents call these with an explicit team_id= (the same signature
    # TenderDB exposes), so both methods must accept it or every result row
    # dies with "unexpected keyword argument 'team_id'".  The proxy is already
    # bound to one team's run, so a caller-supplied team_id is only honoured
    # when given and otherwise falls back to the run's own team.
    def is_duplicate(self, title: str, url: str = "", team_id: str = None) -> bool:
        return self._db.is_cron_duplicate(title, url, team_id=team_id or self.team_id)

    def mark_downloaded(self, title: str, url: str, site: str,
                        keyword: str, published_date: str = "", team_id: str = None):
        self._db.mark_cron_seen(title, url, site, keyword, published_date,
                                team_id=team_id or self.team_id)
