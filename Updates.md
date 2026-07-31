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
