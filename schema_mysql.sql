-- TAiQ — MySQL 8.0+ schema
-- Run this once in your MySQL database before starting TAiQ with DATABASE_URL set.
--
--   mysql -u youruser -p yourdb < schema_mysql.sql
--
-- Prerequisites:
--   - MySQL 8.0.12 or newer
--   - Database created with utf8mb4 charset (important for URL/text indexing):
--       CREATE DATABASE taiq_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
--
-- TAiQ will auto-create these tables on first startup too, but running this
-- script first lets your DBA review and apply it under controlled conditions.

-- ── Dedup table (tracks every tender ever seen, prevents duplicate scraping) ──
CREATE TABLE IF NOT EXISTS downloaded_tenders (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    title_norm      VARCHAR(500) NOT NULL,
    url             VARCHAR(767),
    site            TEXT,
    keyword         TEXT,
    published_date  TEXT,
    downloaded_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_title ON downloaded_tenders (title_norm);
-- url allows multiple NULLs (MySQL treats each NULL as distinct in a UNIQUE index).
CREATE UNIQUE INDEX IF NOT EXISTS idx_url ON downloaded_tenders (url);

-- ── Users and roles ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS users (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      TEXT    NOT NULL,
    email         TEXT    NOT NULL,
    password_hash TEXT    NOT NULL,
    role          TEXT    NOT NULL CHECK(role IN ('superadmin','admin','user')),
    created_by    INTEGER REFERENCES users(id),
    created_at    TEXT    NOT NULL,
    is_active     INTEGER NOT NULL DEFAULT 1,
    UNIQUE KEY uq_username (username(191)),
    UNIQUE KEY uq_email    (email(191))
);

-- ── Manual scrape sessions ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS search_sessions (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    user_id      INTEGER NOT NULL REFERENCES users(id),
    site         TEXT    NOT NULL,
    run_date     TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'running',
    zip_filename TEXT,
    created_at   TEXT    NOT NULL
);
-- run_date is always "YYYY-MM-DD" (10 chars); prefix(10) is sufficient.
CREATE INDEX IF NOT EXISTS idx_sessions_date ON search_sessions (run_date(10));

CREATE TABLE IF NOT EXISTS session_keywords (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    session_id    INTEGER NOT NULL REFERENCES search_sessions(id),
    keyword       TEXT    NOT NULL,
    tenders_found INTEGER NOT NULL DEFAULT 0
);

-- ── Tenders found by manual scrapes ───────────────────────────────────────────
CREATE TABLE IF NOT EXISTS found_tenders (
    id             INT AUTO_INCREMENT PRIMARY KEY,
    session_id     INTEGER NOT NULL REFERENCES search_sessions(id),
    keyword        TEXT    NOT NULL,
    title          TEXT    NOT NULL,
    url            TEXT,
    site           TEXT    NOT NULL,
    published_date TEXT,
    summary_json   TEXT,
    tender_dir     TEXT,
    found_at       TEXT    NOT NULL,
    review_status    VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    reviewed_by      INTEGER,
    reviewed_by_name TEXT,
    reviewed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_found_session ON found_tenders (session_id);

-- ── Tender document attachments ────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS tender_documents (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    tender_id  INTEGER NOT NULL REFERENCES found_tenders(id),
    filename   TEXT    NOT NULL,
    file_path  TEXT    NOT NULL,
    file_type  TEXT,
    file_size  INTEGER,
    stored_at  TEXT    NOT NULL
);

-- ── Audit / activity log ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS activity_logs (
    id           INT AUTO_INCREMENT PRIMARY KEY,
    user_id      INTEGER REFERENCES users(id),
    username     TEXT,
    action       TEXT    NOT NULL,
    details_json TEXT,
    ip_address   TEXT,
    timestamp    TEXT    NOT NULL
);
-- timestamp is always "YYYY-MM-DDTHH:MM:SS" (19 chars); prefix(19) is sufficient.
CREATE INDEX IF NOT EXISTS idx_logs_ts ON activity_logs (timestamp(19));

-- ── TAiQ autonomous agent run history ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cron_runs (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    run_date        TEXT    NOT NULL,
    started_at      TEXT    NOT NULL,
    finished_at     TEXT,
    status          TEXT    NOT NULL DEFAULT 'running',
    total_keywords  INTEGER NOT NULL DEFAULT 0,
    keywords_done   INTEGER NOT NULL DEFAULT 0,
    total_tenders   INTEGER NOT NULL DEFAULT 0,
    error_msg       TEXT,
    stop_requested  INTEGER NOT NULL DEFAULT 0,
    current_keyword TEXT    NOT NULL DEFAULT '',
    log_file        TEXT
);
CREATE INDEX IF NOT EXISTS idx_cron_runs_date ON cron_runs (run_date(10));

-- ── Tenders found by TAiQ ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cron_tenders (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    run_id          INTEGER NOT NULL REFERENCES cron_runs(id),
    keyword         TEXT    NOT NULL,
    title           TEXT    NOT NULL,
    url             TEXT,
    site            TEXT    NOT NULL,
    published_date  TEXT,
    summary_json    TEXT,
    tender_dir      TEXT,
    found_at        TEXT    NOT NULL,
    review_status    VARCHAR(20) NOT NULL DEFAULT 'pending',  -- pending|approved|rejected
    reviewed_by      INTEGER,
    reviewed_by_name TEXT,
    reviewed_at      TEXT
);
CREATE INDEX IF NOT EXISTS idx_cron_tenders_run ON cron_tenders (run_id);

-- ── TAiQ cross-run dedup ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS cron_dedup (
    id              INT AUTO_INCREMENT PRIMARY KEY,
    title_norm      VARCHAR(500) NOT NULL,
    url             VARCHAR(767),
    site            TEXT,
    keyword         TEXT,
    published_date  TEXT,
    found_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cron_dedup_title ON cron_dedup (title_norm);
CREATE UNIQUE INDEX IF NOT EXISTS idx_cron_dedup_url ON cron_dedup (url);

-- ── Tender review comments (approve/reject reasons + discussion) ──────────────
CREATE TABLE IF NOT EXISTS tender_comments (
    id         INT AUTO_INCREMENT PRIMARY KEY,
    source     VARCHAR(20) NOT NULL,   -- 'taiq' (cron_tenders) | 'manual' (found_tenders)
    tender_id  INTEGER     NOT NULL,
    user_id    INTEGER,
    username   TEXT,
    action     VARCHAR(20) NOT NULL DEFAULT 'comment',  -- approved|rejected|comment
    comment    TEXT        NOT NULL,
    created_at TEXT        NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tender_comments_t ON tender_comments (tender_id);
