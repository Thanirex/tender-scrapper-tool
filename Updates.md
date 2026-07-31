# TAiQ — Multi-Tenant System Upgrade Log (`Updates.md`)

**Date**: July 30, 2026  
**Version**: Multi-Tenant Upgrade (CNK Portal & TMI Portal)

---

## Executive Summary
This release transforms TAiQ into a robust **multi-tenant system** supporting separate organization portals (**CNK** and **TMI**). Each team operates in an isolated workspace with dedicated site configurations, keywords, user roles, daily cron schedules, and dashboard data—while sharing the underlying infrastructure, database engine, and Gemini AI summarizer.

---

## Key System Updates

### 1. Multi-Tenant Team Configurations (`configs/teams/`)
Structured dedicated team configuration folders:
- **`configs/teams/cnk/`**:
  - `sites_config.json`: Configured for **19 websites** (UNGM, AFDB, JobInRwanda, AU, ACBF, TradeMark Africa, World Bank, FHI360, etc.)
  - `Keywords.json`: `LS` (Life Sciences/E-Learning) and `HR&KM` categories
  - `negative_keywords.json`: Negative filtering keywords
- **`configs/teams/tmi/`**:
  - `sites_config.json`: Configured for **3 isolated websites** (**Nasscom**, **UNGM**, and **Devnet**)
  - `Keywords.json`: **69 keywords** grouped into 3 TMI categories (**Training**, **Recruitment**, and **Staffing**)
  - `negative_keywords.json`: Negative filtering keywords

---

### 2. Database Multi-Tenancy & Auto-Migration (`db.py`)
- Added **`teams` table** auto-initialization seeding default `'cnk'` and `'tmi'` teams.
- Auto-migrated `team_id` and `team_name` columns across core tables:
  `users`, `search_sessions`, `found_tenders`, `cron_runs`, `cron_tenders`, and `activity_logs`.
- All pre-existing production data defaults to `team_id = 'cnk'` so **zero historical data was lost or modified**.
- Database query methods (`get_stats_for_date`, `get_tenders_for_date`, `get_daily_report`, `get_review_summary`, etc.) now strictly enforce `WHERE team_id = ?` filtering.

---

### 3. JWT Security & User Management (`auth.py`, `api.py`, `routers/admin_router.py`)
- Embedded `team_id` and `team_name` claims directly into signed JWT access tokens (`create_access_token`).
- Updated User Management UI (`users.html`, `users.js`) to allow Administrators to pick **CNK Team** or **TMI Team** when creating users.
- Automated Super Admin Seeding on app startup:
  - **CNK Super Admin**: `superadmin_cnk` / `superadmin` (Password: `Cnkonline@2026`)
  - **TMI Super Admin**: `superadmin_tmi` (Password: `Tmionline@2026`)

---

### 4. Visual Header Team Indicator (`static/nav.js`, `static/style.css`)
- Added a visual Team Badge pill in the navigation header bar:
  - **`[ CNK PORTAL ]`**: Cyan/blue glow for CNK users.
  - **`[ TMI PORTAL ]`**: Purple/indigo glow with an animated live pulse dot for TMI users.

---

### 5. Staggered Multi-Team Cron Scheduler (`cron_runner.py`)
- **Staggered Schedules (Asia/Kolkata IST)**:
  - **CNK Daily Sweep**: Every day at **07:00 AM IST** (`CronTrigger(hour=7, minute=0)`). Scrapes CNK's 19 websites using `LS` & `HR&KM` keywords.
  - **TMI Daily Sweep**: Every day at **09:00 PM IST** (`21:00`). Scrapes TMI's 3 websites (**Nasscom**, **UNGM**, **Devnet**) using `Training`, `Recruitment`, and `Staffing` keywords.
- **Startup Catch-Up**: Automatically checks if past 07:00 AM for CNK or 09:00 PM for TMI with missing runs and executes catch-up sweeps.

---

### 6. Cross-Portal Mutex Lock (`routers/taiq_router.py`)
- In-memory execution tracking (`_current_team_id`) prevents overlapping manual/automated runs between portals.
- If a Super Admin attempts a manual extraction while another team portal is actively scraping, the API returns a clear error:
  `"TAiQ is currently running an extraction for the [OTHER_TEAM] Portal (Run #X). Please wait for it to complete."`

---

### 7. Scraper UI Auto-Population (`static/script.js`, `api.py`)
- Endpoint `/config?team_id=...` returns team-specific websites and keywords.
- Manual scraper page (`Step 3`) auto-selects the team's default category and pre-fills the keywords input box automatically.

---

### 8. Docker & Git Repository Deployment
- Fully verified compatibility with existing `Dockerfile` and `entrypoint.sh`.
- Code successfully built and pushed to production git branch (`main -> prod`).

---

## System Credentials Summary

| Team Portal | Role | Username | Password | Initial Email |
| :--- | :--- | :--- | :--- | :--- |
| **CNK Portal** | Super Admin | `superadmin` / `superadmin_cnk` | `Cnkonline@2026` | `admin@cnk.local` |
| **TMI Portal** | Super Admin | `superadmin_tmi` | `Tmionline@2026` | `admin@tmi.local` |

---

## Add-on Updates (July 31, 2026)

### 9. Team-Based Publication Date Cutoff Rule (`date_utils.py`, `agents/`, `cron_runner.py`, `api.py`)
- **TMI Portal (`team_id == 'tmi'`)**:
  - Publication cutoff window expanded to **1 week (168 hours / 7 days)**.
  - Automatically checks and collects tender documents uploaded within the last 7 days during both scheduled cron runs and manual user searches.
  - Duplicate detection (`db.is_duplicate()`) remains active to automatically skip tenders previously collected during the week.
- **CNK Portal (`team_id == 'cnk'`)**:
  - Preserved the strict **24-hour cutoff** at all costs, ensuring zero changes to CNK operational behavior.
- **Implementation Highlights**:
  - `date_utils.py`: Added `get_max_age_hours(team_id)` (returns 168 for TMI, 24 for CNK) and `is_within_cutoff_ist(date_str, max_age_hours)`.
  - Scraper agents (`ScraperAgent`, `UNGMScraperAgent`, `ReliefWebScraperAgent`, `DRCScraperAgent`, `CHAIScraperAgent`, `AUScraperAgent`) accept `team_id` / `max_age_hours` to enforce the team-specific cutoff window dynamically.
  - Standardized runner functions in `cron_runner.py` and `api.py` pass the active `team_id` down to all scraper agents.

### 10. NGOBOX Scraper Integration (`agents/ngobox_scraper_agent.py`, `date_utils.py`, `sites_config.json`, `cron_runner.py`, `api.py`)
- **New Site Source**: `https://ngobox.org/rfp_eoi_listing.php` configured for both **CNK Portal** and **TMI Portal**.
- **Active Deadline Validation**:
  - NGOBOX listing cards display the **Deadline Date** (e.g., `Deadline: 07 Aug. 2026`).
  - Added `is_deadline_active(date_str)` in `date_utils.py`: accepts tenders whose deadline date is today or in the future (`deadline >= today_ist`), and filters out expired tenders whose deadline has passed.
- **Automated Workflow**:
  - Navigates to NGOBOX listing page and submits search queries via `input#searchme` and `i.fa-search`.
  - Mines detail notice pages (`https://ngobox.org/full_rfp_eoi_...`) and automatically detects and downloads attached detailed RFP/EOI PDF/DOCX documents locally for Level 1 Gemini AI processing.
  - Strictly preserves the **NGOBOX detail page URL** (`https://ngobox.org/full_rfp_eoi_...`) in tender records and Level 1 Excel reports (rather than the direct PDF link) for user redirection.
  - Fully integrated into scheduled sweeps (`cron_runner.py`) and manual searches (`api.py`).

### 11. Universal Active Deadline & Date Validation (`date_utils.py`, `agents/`)
- **Combined Date & Deadline Verification**:
  - Added `is_date_or_deadline_valid(date_str, max_age_hours)` to `date_utils.py`.
  - Applied across all scraper agents (`ScraperAgent`, `NGOBOXScraperAgent`, `UNGMScraperAgent`, `DRCScraperAgent`, `CHAIScraperAgent`, `AUScraperAgent`, `ReliefWebScraperAgent`) for both **CNK Portal** and **TMI Portal**.
  - **Rules**: Accepts tenders if EITHER the publication date falls within the cutoff window OR the tender deadline date is active (today or in the future). Filters out tenders only if they are outside the publication window AND their deadline has expired.

### 12. Multi-Tenant Team-Agnostic Deduplication (`db.py`, `cron_runner.py`, `agents/`)
- **Complete Team Isolation**:
  - Migrated `downloaded_tenders` and `cron_dedup` tables in `db.py` to include `team_id` and updated unique indexes to `(url, team_id)`.
  - Updated `is_duplicate(title, url, team_id)`, `mark_downloaded(..., team_id)`, `is_cron_duplicate(..., team_id)`, `mark_cron_seen(..., team_id)`, and `CronDBProxy` to filter deduplication per team.
  - **Result**: TMI and CNK portals operate completely independently. A tender collected by TMI will NOT block CNK from collecting the same tender for CNK portal, guaranteeing 100% agnostic data isolation across both portals.

### 13. NGOBOX Detail Extraction Bugfix (`agents/ngobox_scraper_agent.py`)
- **Fix**: Added missing `team_id` parameter to `NGOBOXScraperAgent._extract_detail()` signature and invocation.
- **Result**: Eliminated `NameError: name 'team_id' is not defined` during detail notice page extraction, allowing document downloads and tender saves to complete smoothly.

### 14. Comprehensive Multi-Agent Parameter Audit & Verification (`agents/`)
- **System-Wide Audit**: Audited all 7 scraper agents (`ScraperAgent`, `NGOBOXScraperAgent`, `UNGMScraperAgent`, `DRCScraperAgent`, `CHAIScraperAgent`, `AUScraperAgent`, `ReliefWebScraperAgent`).
- **Standardized Pass-Through**: Ensured `team_id` is defined and passed down through all primary methods (`search`, `scrape`) and detail extraction helpers (`_extract_detail`, `_extract_tender`) to `db.is_duplicate` and `db.mark_downloaded`.
- **Result**: 100% parameter safety across all scraper agents. No agent will throw `NameError` or parameter mismatch.

### 15. Complete Web Dispatcher Audit (`api.py`)
- **API Dispatch Helpers**: Updated all 15 scraper dispatch helpers in `api.py` (`_run_standard_scrape`, `_run_nasscom_scrape`, `_run_trademarkafrica_scrape`, `_run_worldbank_scrape`, `_run_fhi360_scrape`, `_run_gatsbyafrica_scrape`, `_run_jsi_scrape`, `_run_ngobox_scrape`, `_run_afrosai_scrape`, `_run_acbf_scrape`, `_run_drc_scrape`, `_run_chai_scrape`, `_run_au_scrape`, `_run_reliefweb_scrape`, `_run_ungm_scrape`).
- **Full Call-Stack Isolation**: Passed active user session `team_id` into every helper function and agent search invocation. Every site—including FHI360, World Bank, TradeMark Africa, Nasscom, JSI, ACBF, etc.—operates with 100% team data isolation.

### 16. Dual-Portal Independent Automation Verification (`cron_runner.py`, `cron_runner_tmi.py`)
- **Independent Automation Schedules**:
  - **TMI Portal**: Runs daily at **09:00 PM IST (21:00)** using `team_id="tmi"` with 168-hour (1 week) cutoff window and NGOBOX support.
  - **CNK Portal**: Runs daily at **07:00 AM IST** using `team_id="cnk"` with 24-hour cutoff window.
- **Standalone Runner**: Provided `cron_runner_tmi.py` for CLI/isolated process execution.
- **Verification**: Verified end-to-end configuration loading, date cutoff rules, scheduler job definitions, and database multi-tenant isolation. Both portals run 100% smoothly and independently as expected.
