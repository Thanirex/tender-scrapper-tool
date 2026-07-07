# TAiQ — Tender AI Intelligence Platform

**TAiQ** is a full-stack procurement intelligence platform that autonomously monitors tender portals, analyses opportunities with a large language model, and surfaces structured intelligence to your team — all from a role-gated web dashboard.

It started as a scraper. It became an autonomous agent with a command centre.

---

## What it does

| Capability                          | Description                                                                                                                                                                                                                     |
| ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Autonomous daily agent**    | TAiQ runs at 07:00 IST every day without human intervention — scraping every configured site across every keyword, analysing each tender with Gemini, and populating the database before your team's morning standup           |
| **Multi-site scraping**       | UNGM (UN Global Marketplace, auth-based), DevNet, NGOBox — new sites added via a JSON config, no code changes                                                                                                                  |
| **LLM extraction**            | 26-field structured Level 1 analysis per tender: reference, deadline, scope, eligibility, budget, selection criteria, consortium rules, clarification contacts, and more                                                        |
| **Confidence-graded output**  | Every field is tagged`[VERIFIED]` (scraped from HTML ground truth), `[EXTRACTED]` (found by LLM in documents), `[NOT_FOUND]`, or `[MISMATCH: HIGH PRIORITY ERROR]` when a verified field contradicts attached documents |
| **Document intelligence**     | Downloads all PDF, DOCX, and XLSX attachments per tender; extracts text from all of them before the LLM sees the bundle                                                                                                         |
| **Manual scraping**           | Any authorised user can trigger a targeted scrape — choose site, keyword category or custom keywords, and watch a live log stream                                                                                              |
| **Excel reports**             | Level 1 Excel report generated per tender run, downloadable from the dashboard                                                                                                                                                  |
| **Dashboard with calendar**   | Select any historical date; see run activity, per-site tender counts, proportional bar charts, and the full tender grid with keyword and site filters                                                                           |
| **Role-based access control** | Three roles —`user` (read-only), `admin` (scrape + dashboard), `superadmin` (user management, audit log, stop TAiQ mid-run)                                                                                              |
| **Audit trail**               | Every login, scrape, and admin action is logged with timestamp and user                                                                                                                                                         |
| **Zero-credential storage**   | UNGM login credentials for manual scrapes are never persisted — used in-session only                                                                                                                                           |
| **Docker-native**             | Single`docker compose up` command; all data persisted to a named volume                                                                                                                                                       |

---

## Architecture

```
Browser  (login → dashboard → TAiQ work → users → audit)
    │  REST + WebSocket
    ▼
FastAPI  (api.py)
    │
    ├── Auth          JWT RS256 · bcrypt · 7-day tokens · role middleware
    │
    ├── Manual scrape  WebSocket /ws/scrape
    │     ScraperAgent      → DevNet / NGOBox  (Playwright, CSS-selector config)
    │     UNGMScraperAgent  → UNGM             (Playwright + login + attachment download)
    │     FileReader        → PDF / DOCX / XLSX / TXT
    │     SummarizerAgent   → Gemini (gemma-4-31b-it), 500k char window, retry on 429
    │     ExcelWriter       → Level 1 .xlsx per run
    │
    ├── TAiQ autonomous agent  (cron_runner.py)
    │     APScheduler CronTrigger 07:00 IST
    │     Phase 1: UNGM  (UNGMScraperAgent, all keywords)
    │     Phase 2+: standard sites from sites_config.json (ScraperAgent, per keyword)
    │     CronDBProxy    dedup — skips tenders already seen this run
    │     Live log buffer (in-memory during run, written to disk after)
    │     Stop signal    superadmin can halt mid-run from dashboard
    │
    ├── Dashboard API   /dashboard/stats · /dashboard/tenders · /dashboard/dates
    │     Merges manual sessions + TAiQ cron runs into a unified timeline
    │
    ├── TAiQ API        /taiq/status · /taiq/run · /taiq/stop · /taiq/logs
    │
    ├── Admin API       user CRUD · role changes
    │
    └── Superadmin API  audit log · platform-wide controls
  
MySQL / SQLite  (tender_tracker.db or DATABASE_URL)
    tables: users · sessions · found_tenders · cron_runs · cron_tenders · audit_log
```

---

## Roles

| Role           | Can do                                                                    |
| -------------- | ------------------------------------------------------------------------- |
| `user`       | View dashboard, filter tenders                                            |
| `admin`      | All of`user` + trigger manual scrapes, view live logs, download reports |
| `superadmin` | All of`admin` + manage users, view audit log, stop TAiQ mid-run         |

The first `superadmin` is seeded from environment variables on startup. All subsequent users are created by a superadmin from the Users page.

---

## Quick start (Docker)

```bash
# 1. Copy and fill in credentials
cp .env.example .env
# Edit .env — set GEMINI_API_KEY, UNGM_EMAIL, UNGM_PASSWORD, JWT_SECRET_KEY, SUPERADMIN_PASSWORD

# 2. Build and start
docker compose up -d

# 3. Open
open http://localhost:8001
```

Log in with the superadmin credentials you set in `.env`. TAiQ will fire its first run automatically if it is past 07:00 IST.

---

## Environment variables

| Variable                | Required              | Description                                                                                                                                      |
| ----------------------- | --------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------ |
| `GEMINI_API_KEY`      | Yes                   | Google AI Studio key — used for all LLM summarisation                                                                                           |
| `UNGM_EMAIL`          | Yes (for UNGM)        | UNGM vendor portal login email                                                                                                                   |
| `UNGM_PASSWORD`       | Yes (for UNGM)        | UNGM vendor portal password                                                                                                                      |
| `JWT_SECRET_KEY`      | **Yes in prod** | Secret for signing JWTs — use a long random string                                                                                              |
| `SUPERADMIN_USERNAME` | No                    | Username for the seeded superadmin (default:`superadmin`)                                                                                      |
| `SUPERADMIN_EMAIL`    | No                    | Email for the seeded superadmin                                                                                                                  |
| `SUPERADMIN_PASSWORD` | No                    | Password for the seeded superadmin (default:`changeme123` — change this)                                                                      |
| `TENDER_DATA_DIR`     | No                    | Override data directory (default:`~/Documents/Tender Scrapping Documents`) — set to `/data` in Docker                                       |
| `DATABASE_URL`        | No                    | Database connection string — leave unset for SQLite, or set to`mysql+pymysql://user:pass@host:3306/db` for MySQL (see Database section below) |

---

## Adding a new site

Edit `sites_config.json`:

```json
{
  "ungm":   { "url": "https://www.ungm.org", "requires_auth": true },
  "devnet": {
    "url": "https://...",
    "search_input_selector": "input#search",
    "search_button_selector": "button[type=submit]",
    "results_link_selector": "a.tender-link",
    "tender_title_selector": "h1.tender-title",
    "tender_description_selector": "div.tender-body"
  }
}
```

Any site without `"requires_auth": true` is picked up by the autonomous TAiQ agent automatically on the next run — no code changes needed.

---

## Keyword categories

Edit `Keywords.json`:

```json
{
  "E-Learning": ["Learning Management System", "LMS", "eLearning Platform"],
  "Analytics":  ["Dashboard", "Data Visualization", "Business Intelligence"]
}
```

Categories appear in the manual scrape UI dropdown. All keywords across all categories are used by the TAiQ daily agent.

---

## Data directory layout

```
$TENDER_DATA_DIR/
  tender_tracker.db                       SQLite database
  cron_logs/
    run_<id>.log                          Full log for each TAiQ run
  downloads/
    cron/
      cron_<timestamp>/
        ungm/
          <keyword>/
            <tender_title>/               Attachments (PDF, DOCX, XLSX)
            Level1_<title>.xlsx           Level 1 analysis
        devnet/  ngobox/ ...              Same structure per site
    <site>/
      <keyword>_<n>_<title>.txt           Raw page text (manual runs)
  outputs/
    Scrape_Results_<timestamp>.xlsx       Manual run Excel reports
```

---

## Local development

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium

cp .env.example .env               # fill in your keys

uvicorn api:app --reload --port 8001
```

Open `http://localhost:8001`.

---

## Database

TAiQ supports **MySQL 8.0+** (recommended for production), **SQLite** (zero-setup, good for a single server), and **PostgreSQL**. The backend is selected entirely through the `DATABASE_URL` environment variable — no code changes needed.

---

### Option A — MySQL 8.0+ (recommended for production)

This is the standard deployment option. Your DBA creates the database once; TAiQ connects on every startup.

**Step 1 — create the database and user** (run as MySQL root or DBA):

```sql
CREATE DATABASE taiq_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'taiq_user'@'%' IDENTIFIED BY 'strong-password-here';
GRANT ALL PRIVILEGES ON taiq_db.* TO 'taiq_user'@'%';
FLUSH PRIVILEGES;
```

> The `utf8mb4` charset is required. TAiQ stores Unicode tender titles and URLs — the default `latin1` will corrupt them.

**Step 2 — apply the schema** (run once before the first TAiQ startup):

```bash
mysql -h db-server.company.com -u taiq_user -p taiq_db < schema_mysql.sql
```

TAiQ will also attempt to auto-create missing tables on startup, but running the schema file first gives your DBA visibility into what is being created and prevents permission surprises.

**Step 3 — set `DATABASE_URL` in `.env`**:

```
DATABASE_URL=mysql+pymysql://taiq_user:strong-password-here@db-server.company.com:3306/taiq_db
```

**Step 4 — rebuild and start**:

```bash
docker compose build --no-cache
docker compose up -d
```

TAiQ connects to MySQL on startup. The `pymysql` driver is already in `requirements.txt` — no extra packages needed.

---

### Option B — SQLite (zero-setup, single-server deployments)

No database server required. TAiQ writes to a single file inside the Docker volume.

```
TENDER_DATA_DIR=/data          # already set in .env.example
# DATABASE_URL not set         # TAiQ uses SQLite at /data/tender_tracker.db
```

Back up the file at any time:

```bash
docker run --rm -v taiq-data:/data -v $(pwd):/backup alpine \
  tar czf /backup/taiq-backup.tar.gz /data
```

SQLite also works over a network share as long as only one TAiQ container writes at a time:

```
DATABASE_URL=sqlite:////mnt/company-share/taiq/taiq.db
```

---

### Option C — PostgreSQL

```sql
-- Run as postgres superuser
CREATE DATABASE taiq_db;
CREATE USER taiq_user WITH PASSWORD 'strong-password-here';
GRANT ALL PRIVILEGES ON DATABASE taiq_db TO taiq_user;
```

```bash
psql -U taiq_user -d taiq_db -f schema.sql
```

```
DATABASE_URL=postgresql://taiq_user:strong-password-here@db-server.company.com:5432/taiq_db
```

Uncomment `psycopg2-binary` in `requirements.txt`, then rebuild:

```bash
docker compose build --no-cache && docker compose up -d
```

---

### Schema reference

| Table                  | Contents                                             |
| ---------------------- | ---------------------------------------------------- |
| `users`              | Accounts, roles, bcrypt password hashes              |
| `search_sessions`    | One row per manual scrape run                        |
| `found_tenders`      | Tenders discovered by manual scrapes                 |
| `cron_runs`          | One row per TAiQ autonomous agent run                |
| `cron_tenders`       | Tenders discovered by TAiQ                           |
| `cron_dedup`         | Cross-run deduplication index for TAiQ               |
| `downloaded_tenders` | Global dedup index (manual scrapes)                  |
| `activity_logs`      | Audit trail — every login, scrape, and admin action |

MySQL DDL is in `schema_mysql.sql`. PostgreSQL / SQLite DDL is in `schema.sql`.

---

## Project structure

```
api.py                      FastAPI app, static routes, superadmin seed
auth.py                     JWT encode/decode, bcrypt, role middleware
db.py                       TenderDB (SQLite / MySQL / PostgreSQL via DATABASE_URL), CronDBProxy
schema_mysql.sql            MySQL 8.0+ DDL — run once before first startup with a MySQL DATABASE_URL
schema.sql                  PostgreSQL / SQLite DDL
cron_runner.py              TAiQ autonomous agent, APScheduler, stop signal
date_utils.py               IST-aware date helpers
paths.py                    Centralised path config, TENDER_DATA_DIR support

agents/
  scraper_agent.py          Playwright scraper for DevNet / NGOBox
  ungm_scraper_agent.py     Playwright scraper for UNGM (auth + attachments)
  file_reader.py            PDF / DOCX / XLSX / TXT text extractor
  summarizer_agent.py       Gemini LLM wrapper, Level 1 field extraction
  excel_writer.py           Level 1 .xlsx report generator
  doc_writer.py             Word .docx report generator (UNGM manual runs)

routers/
  auth_router.py            /auth/login, /auth/me
  admin_router.py           /scrape, /ws/scrape, /download/*
  dashboard_router.py       /dashboard/stats, /tenders, /dates
  taiq_router.py            /taiq/status, /run, /stop, /logs, /history
  superadmin_router.py      /admin/users, /audit

static/
  login.html / auth.js      Login page + JWT storage
  nav.js                    Shared nav bar, role-aware links
  index.html / script.js    Manual scrape UI
  dashboard.html / .js      Analytics dashboard with calendar
  taiq.html / taiq.js       TAiQ work page — history, logs, run controls
  users.html / users.js     User management (superadmin)
  audit.html / audit.js     Audit log viewer (superadmin)
  style.css                 Dark glassmorphism design system

sites_config.json           Site selector config — add new sites here
Keywords.json               Keyword categories for the scrape UI
Dockerfile
docker-compose.yml
entrypoint.sh
.env.example
```

---

## Tech stack

| Layer              | Technology                                                    |
| ------------------ | ------------------------------------------------------------- |
| Backend            | Python 3.12 · FastAPI · Uvicorn                             |
| Browser automation | Playwright (Chromium headless)                                |
| LLM                | Google Gemini —`gemma-4-31b-it`                            |
| Scheduling         | APScheduler (CronTrigger)                                     |
| Database           | MySQL 8.0+ (prod) · SQLite (dev/single-server) · PostgreSQL |
| Auth               | JWT (`python-jose`) · bcrypt                               |
| Document parsing   | pdfplumber · python-docx · openpyxl                         |
| Container          | Docker · Docker Compose                                      |
| Frontend           | Vanilla JS · CSS custom properties (no build step)           |
