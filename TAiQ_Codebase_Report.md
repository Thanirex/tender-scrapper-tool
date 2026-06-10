# TAiQ — Technical Codebase Analysis & Architecture Report

Welcome to the comprehensive technical report for **TAiQ** (Tender AI Intelligence Platform). This document provides an in-depth breakdown of the codebase architecture, data schemas, scraping agents, LLM processing pipeline, and frontend user interface.

---

## 1. Executive Summary

**TAiQ** is a production-ready, full-stack procurement intelligence platform designed to autonomously monitor tender portals, download and parse associated procurement documentation, perform Level-1 structured analysis using a Large Language Model (Gemini), and expose a role-gated web dashboard for team collaboration.

### Key Capabilities
* **Autonomous Daily Execution:** Runs automatically at 07:00 IST every day via an embedded scheduler. It queries all configured sites for a predefined set of keywords and updates the database before business hours.
* **Multi-Site Extensibility:** Supports scraping public portals (e.g., DevNet, NGOBox) dynamically via JSON selector configurations, alongside highly specialized, authentication-guarded portals (e.g., UN Global Marketplace).
* **Document Extraction & LLM Summarization:** Downloads and extracts text from PDFs, DOCX, XLSX, and TXT files, combining them to feed Google Gemini for structured Level-1 analysis.
* **Excel Report Compilation:** Generates auto-styled, color-coded Level-1 Excel report spreadsheets for individual tenders or aggregated search runs.
* **Role-Based Access Control (RBAC):** Features token-based JWT security with three user roles: `user` (read-only), `admin` (can trigger scrapes), and `superadmin` (can manage users, view audit trails, and halt running automation).
* **Cross-Dialect Database Layer:** Native support for SQLite, MySQL, and PostgreSQL using a custom unified connection wrapper without requiring an ORM.

---

## 2. Architectural Overview

The TAiQ architecture consists of a FastAPI backend serving HTML/JS static files, communicating via a REST API and WebSockets for live status updates, interacting with standard/secure Playwright agents, and persisting data to a relational database.

```
                    ┌──────────────────────────────────────────┐
                    │            Browser Frontend              │
                    │   (HTML5 / Vanilla JS / CSS Variables)   │
                    └──────────────┬────────────────────▲──────┘
                                   │                    │
                            REST   │                    │ Live WebSockets
                         Requests  │                    │ Log & Result Streams
                                   ▼                    │
                    ┌───────────────────────────────────┴──────┐
                    │             FastAPI Server               │
                    │               (api.py)                   │
                    └──────┬──────────────┬──────────────┬─────┘
                           │              │              │
                           ▼              ▼              ▼
                    ┌────────────┐ ┌────────────┐ ┌─────────────┐
                    │ Auth Engine│ │ Scheduler  │ │   Routers   │
                    │ (auth.py)  │ │(cron_runner)││ (routers/*) │
                    └────────────┘ └──────┬─────┘ └─────────────┘
                                          │
                                          ▼
                                   ┌────────────┐
                                   │  Scraper   │
                                   │ Orchestrator
                                   └──────┬─────┘
                                          │
                     ┌────────────────────┼───────────────────┐
                     ▼                    ▼                   ▼
            ┌─────────────────┐  ┌─────────────────┐  ┌───────────────┐
            │ Playwright Agent│  │   UNGM Agent    │  │  File Reader  │
            │(scraper_agent.py│  │(ungm_scraper_agt│  │(file_reader.py│
            └────────┬────────┘  └────────┬────────┘  └───────┬───────┘
                     │                    │                   │
                     └────────────────────┼───────────────────┘
                                          │
                                          ▼
                                 ┌─────────────────┐
                                 │Summarizer Agent │ ──► Google Gemini API
                                 │(summarizer_agent│
                                 └────────┬────────┘
                                          │
                                          ▼
                                 ┌─────────────────┐
                                 │  Excel Writer   │ ──► File Output (.xlsx)
                                 │(excel_writer.py)│
                                 └────────┬────────┘
                                          │
                                          ▼
                                 ┌─────────────────┐
                                 │  Database URL   │ ──► SQLite / MySQL / PG
                                 │    (db.py)      │
                                 └─────────────────┘
```

---

## 3. Core Backend Components

### 3.1 API Gateway & Entrypoint (`api.py`)
[api.py](file:///c:/Users/Thaneesh/Documents/GitHub/Tender%20Scrapper%20tool%20old/api.py) serves as the system gateway. It performs the following functions:
* **Lifecycle Management:** Instantiates the database connection wrapper and initializes the APScheduler background thread on startup, and tears it down gracefully on shutdown.
* **Routing:** Integrates distinct sub-routers for authentication, administrative scraper controls, dashboard stats, superadmin tasks, and scheduler management.
* **Static File Mounts:** Mounts local folders `/static` and `/assets` to serve client files.
* **Scraper Threads:** Spawns manual scraper tasks inside background worker threads to avoid blocking FastAPI's async event loop.
* **Live WebSocket Channel (`/ws/scrape`):** Establishes bi-directional communication with administrators triggering manual scrapes, providing real-time console log streams and incremental progress results.

### 3.2 Authentication Module (`auth.py`)
[auth.py](file:///c:/Users/Thaneesh/Documents/GitHub/Tender%20Scrapper%20tool%20old/auth.py) governs the platform's security boundaries:
* **Hashing:** Utilizes `bcrypt` salt generation for securing passwords stored in the database.
* **JWT Tokens:** Generates cryptographic signature tokens signed with `HS256` and an environment-configurable `JWT_SECRET_KEY`, expiring after 7 days.
* **Role Verification:** Implements the `require_roles(*roles)` FastAPI dependency middleware, restricting endpoints to authorized personnel (e.g., restricting user creation to `superadmin`).

### 3.3 Database Layer (`db.py`)
[db.py](file:///c:/Users/Thaneesh/Documents/GitHub/Tender%20Scrapper%20tool%20old/db.py) handles storage and schema migrations:
* **Dialect Normalization:** Dynamically detects whether to connect to SQLite, MySQL, or PostgreSQL depending on the configured `DATABASE_URL`. It auto-adjusts primary key formats (`AUTOINCREMENT` vs `AUTO_INCREMENT` vs `SERIAL`), indices, and varchar column limits.
* **Custom Driver Connection Context:** Implements a unified connection context manager `_Conn` that wraps `sqlite3`, `psycopg2`, and `pymysql` transactions, standardizing parameter placeholder mappings (`?` vs `%s`) and row mappings.
* **Deduplication Logic:** Implements normalized text stripping for title similarity matches alongside strict URL comparisons to prevent downloading or analyzing duplicate tenders.
* **Cron Proxy:** Implements the `CronDBProxy` pattern, redirecting queries to automated-run deduplication tables so that manual scrapes and daily runs do not corrupt each other's indexes.

### 3.4 Scheduler Engine (`cron_runner.py`)
[cron_runner.py](file:///c:/Users/Thaneesh/Documents/GitHub/Tender%20Scrapper%20tool%20old/cron_runner.py) handles automated workflow scheduling:
* **Cron Schedules:** Configured with `APScheduler` to fire a daily job at 07:00 IST (Indian Standard Time).
* **Startup Catch-Up Check:** Evaluates on startup if the day's 07:00 IST marker has passed without an active or completed run, spawning a catch-up execution immediately if needed.
* **Graceful Stopping:** Monitors a platform-wide stop signal event flag. If a `superadmin` clicks "Stop" in the dashboard, the runner catches the event, aborts the active loop, cleans up Playwright windows, updates database states, and flushes log files to disk.
* **In-Memory Logging:** Intercepts agent print statements and logs them in an memory-mapped thread-safe buffer, exposing them to administrative WebSocket readers before writing them out to `.log` archives.

---

## 4. Agent-Oriented AI Scraper Engine

TAiQ relies on a specialized workflow composed of Playwright scrapers, document extractors, and LLM analyzers:

```
                  ┌────────────────────────────────────────┐
                  │          1. Web Scraper Agent          │
                  │   Navigate to list → Extract Details   │
                  └───────────────────┬────────────────────┘
                                      │
                                      ▼
                  ┌────────────────────────────────────────┐
                  │          2. File Reader Agent          │
                  │   Download PDFs/Word/Excel → Extract   │
                  └───────────────────┬────────────────────┘
                                      │
                                      ▼
                  ┌────────────────────────────────────────┐
                  │       3. Summarizer Agent (LLM)        │
                  │   Construct Prompt → Gemini Inference   │
                  └───────────────────┬────────────────────┘
                                      │
                                      ▼
                  ┌────────────────────────────────────────┐
                  │          4. Excel Writer Agent         │
                  │  Compile Level-1 Sheets → Output File  │
                  └────────────────────────────────────────┘
```

### 4.1 Standard Site Scraper (`agents/scraper_agent.py`)
[scraper_agent.py](file:///c:/Users/Thaneesh/Documents/GitHub/Tender%20Scrapper%20tool%20old/agents/scraper_agent.py) is a general-purpose web crawler:
* **Playwright Automation:** Launches a headless Chromium browser instance to dynamically query search forms.
* **JSON Site Mapping:** Resolves target elements dynamically from CSS selector maps stored in `sites_config.json`.
* **Relevance Checks:** Filters out tenders where the query keyword is absent from the page title.
* **Age-based Filtering:** Evaluates publication timestamps, discarding records older than 24 hours (unless the site config explicitly bypasses the date filter).
* **Caching:** Saves the raw HTML notice page as `page_content.txt` in a local directory cache to ensure auditable record keeping.

### 4.2 UNGM Scraper (`agents/ungm_scraper_agent.py`)
[ungm_scraper_agent.py](file:///c:/Users/Thaneesh/Documents/GitHub/Tender%20Scrapper%20tool%20old/agents/ungm_scraper_agent.py) handles the complex UN Global Marketplace portal:
* **Stateful Sessions:** Automates form inputs to log in with vendor credentials, monitoring page changes instead of relying on hardcoded delays.
* **Active Notice Filters:** Programmatically updates AJAX query controls to filter for active notices without resetting other inputs.
* **Quantum Portal Integration:** 
  1. Identifies secure Oracle/SharePoint-based procurement systems (such as UNDP Quantum).
  2. Detects verification tokens and navigates authentication portals to register the scraping session.
  3. Hovers over hidden list components, toggles document select-all elements, triggers zip actions, and registers download callbacks.
* **Decompression Pipeline:** Decompresses multi-layered ZIP downloads recursively, truncating file names on-the-fly to prevent Windows path length violations (`MAX_PATH` exceeding 260 characters).

### 4.3 Document Parser (`agents/file_reader.py`)
[file_reader.py](file:///c:/Users/Thaneesh/Documents/GitHub/Tender%20Scrapper%20tool%20old/agents/file_reader.py) extracts text content from attachments:
* **PDFs (`pdfplumber`):** Extracts body copy page-by-page. Critically, it extracts PDF table structures, serializing cells with a pipe separator (`|`) to preserve tabular structure (which often contains eligibility and budget tables).
* **Word Documents (`docx`):** Interleaves paragraphs and tables in their correct visual sequence by reading the XML document elements.
* **Excel Sheets (`openpyxl`):** Iterates over active sheets, formatting grid cell lines to retain tabular data structures.
* **Fail-safes:** Catches encoding issues or corrupted downloads, appending error summaries to the text dump so that the downstream LLM understands that an attachment failed to parse.

### 4.4 Summarization Agent (`agents/summarizer_agent.py`)
[summarizer_agent.py](file:///c:/Users/Thaneesh/Documents/GitHub/Tender%20Scrapper%20tool%20old/agents/summarizer_agent.py) orchestrates the LLM:
* **Gemini SDK Integration:** Uses the modern Google Gemini API (`genai.Client`) targeting the `gemma-4-31b-it` model.
* **Clean Text Inputs:** Strips null bytes and control characters to prevent gRPC serialization errors.
* **Token Budget Control:** Truncates combined page-and-document text inputs (typically capping at 120,000 characters) to stay safely within Gemini's rate limits.
* **Rate-Limit Backoff:** Catches HTTP 429 and `RESOURCE_EXHAUSTED` exceptions, applying a linear retry backoff strategy (up to 3 times, waiting over 60 seconds) to ensure scraping runs complete successfully.
* **Confidence Grading:** Forces the LLM to output a metadata tag for every field:
  * `[VERIFIED]`: Scraped directly from structured HTML (authoritative).
  * `[EXTRACTED]`: Discovered in unstructured documents.
  * `[NOT_FOUND]`: Absent from the data.
  * `[MISMATCH: HIGH PRIORITY ERROR]`: Scraped fields contradict the text in documents.
* **Level-1 Prompt Mapping:** Instructs the LLM to output key-value lines corresponding to 26 specific procurement fields, including reference numbers, budget (quantum), eligibility, scope of work, and clarification contacts.
* **Parsing Regex:** Parses raw text outputs back into standard Python dictionaries, handling multi-line outputs and removing markdown markers.

### 4.5 Excel Report Compiler (`agents/excel_writer.py`)
[excel_writer.py](file:///c:/Users/Thaneesh/Documents/GitHub/Tender%20Scrapper%20tool%20old/agents/excel_writer.py) creates client reports:
* **Unified Template:** Generates spreadsheets matching TMI's corporate styling guidelines.
* **Palette Coloring:** Uses consistent hex fills (dark navy headers, medium blue for sections, light blue for identifiers, dark green for validation, soft yellow for deadlines).
* **Grid Formatting:** Automatically wraps long scope statements, configures fixed column widths, and dynamically updates row heights to prevent squished text.
* **Sheet Naming:** Normalizes tender titles into safe 31-character strings (removing invalid characters like `\ / : * ? [ ]`) and handles naming collisions.

---

## 5. Database Schema & Tables

The schema is divided into user management, manual scrape tracking, automated scheduler logs, and audit logs. Below is a reference of the primary tables:

| Table Name | Primary Purpose | Key Fields |
| :--- | :--- | :--- |
| `users` | User credentials & roles | `id`, `username`, `email`, `password_hash`, `role`, `is_active` |
| `search_sessions` | Tracking manual scrape runs | `id`, `user_id`, `site`, `run_date`, `status`, `zip_filename` |
| `session_keywords` | Keywords searched per manual session | `id`, `session_id`, `keyword`, `tenders_found` |
| `found_tenders` | Individual tenders found manually | `id`, `session_id`, `keyword`, `title`, `url`, `summary_json`, `tender_dir` |
| `tender_documents` | Attachments downloaded for manual tenders | `id`, `tender_id`, `filename`, `file_path`, `file_type`, `file_size` |
| `cron_runs` | Logs of daily automated scheduler runs | `id`, `run_date`, `started_at`, `finished_at`, `status`, `total_tenders` |
| `cron_tenders` | Individual tenders found by the daily scheduler | `id`, `run_id`, `keyword`, `title`, `url`, `summary_json`, `tender_dir` |
| `cron_dedup` | Deduplication index for scheduler runs | `id`, `title_norm`, `url`, `site`, `found_at` |
| `downloaded_tenders` | Global deduplication index for manual runs | `id`, `title_norm`, `url`, `site`, `downloaded_at` |
| `activity_logs` | Audit trail of all administrative actions | `id`, `user_id`, `username`, `action`, `details_json`, `ip_address` |

### Database Optimization Features
* **Indices:** Includes composite indices on date columns (`run_date`, `timestamp`) and lookup keys (`session_id`, `run_id`).
* **Title Normalization:** Indexes a lowercase, alphanumeric-only version of tender titles (`title_norm`) for fast, language-independent similarity matches.
* **Partial Indices:** Implements partial uniqueness constraints on SQLite and PostgreSQL (`WHERE url IS NOT NULL AND url != ''`) to support multiple empty fields while ensuring strict uniqueness for valid URLs.

---

## 6. Frontend Web UI Layout (`static/`)

The frontend is a single-page style client interface built with vanilla JavaScript, HTML5, and CSS custom properties (no build/bundling step needed).

* **Design System (`style.css`):** Features a premium dark glassmorphism aesthetic. It uses backdrop filters, subtle glow boundaries, HSL color tokens, and hover micro-animations.
* **Navigation Orchestration (`nav.js`):** Inspects client JWT tokens, dynamically building the sidebar layout depending on the user's role (e.g., hiding user management links from read-only users).
* **Manual Scraper Panel (`index.html` / `script.js`):** Integrates keyword selection, inputs credentials, starts WebSockets, renders live logs, and lists results.
* **Historical Dashboard (`dashboard.html` / `dashboard.js`):** Includes a datepicker calendar to view historical runs, site-by-site tender distributions, and search grids with live client filters.
* **Audit Trail Portal (`audit.html` / `audit.js`):** Exposes security events, login histories, and actions taken across the system.
* **User Control (`users.html` / `users.js`):** Simple interface for administrators to create, edit, activate, or suspend accounts.
* **Scheduler Work Center (`taiq.html` / `taiq.js`):** Displays scheduler logs, allows manual scheduler runs, and triggers emergency stop controls.

---

## 7. Configuration & Deployment

### 7.1 Extension Configs
* **`sites_config.json`:** Easily add support for new sites. Adding a site entry with CSS selectors automatically registers the portal. Bypassing `"requires_auth": true` automatically includes the new portal in the daily scheduled run.
* **`Keywords.json`:** Holds keyword groupings. Adding new array items automatically includes them in daily scheduler sweeps and maps them to manual drop-downs.

### 7.2 Docker Deployment
The project is containerized using a clean `Dockerfile` and `docker-compose.yml`:
* **Environment variables:** Configures `GEMINI_API_KEY`, credentials, and custom database URLs (`DATABASE_URL`).
* **Volume Persistence:** Mounts a local volume `/data` mapped to `TENDER_DATA_DIR` so that downloaded documents, logs, and SQLite databases persist across container restarts.
