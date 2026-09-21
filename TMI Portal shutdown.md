# TMI Portal shutdown

**Status:** ⏸️ PAUSED since **2026-09-11**
**Why:** The TMI team is not actively using the portal right now. They asked for something to be
done on our side, but we don't have the bandwidth at the moment, so the portal is switched off
until they ask for it again.

---

## What is switched off

| Service | Behaviour while paused |
|---|---|
| **TMI login** | TMI users (incl. `superadmin_tmi`) get: *"The TMI portal is paused for now. Please contact the CNK team to have it turned back on."* |
| **Open TMI sessions** | Tokens are valid for 7 days, so they are rejected on every request instead. Open TMI tabs get bounced to the login page. |
| **Daily TMI scrape (09:00 PM IST)** | Not scheduled at all. |
| **Startup catch-up for TMI** | Disabled (a restart after 9 PM no longer fires a TMI run). |
| **Manual TMI runs** | TAiQ "Run" page, manual `/ws/scrape`, and `python cron_runner_tmi.py` all refuse. The log says *"Run for team tmi skipped — portal is paused"*. |

**CNK is not affected.** Its 07:00 AM run, logins and pages work exactly as before.

## What is kept (nothing was deleted)

- TMI users and their passwords (`users` table, `team_id = 'tmi'`)
- All TMI tenders, run history, reviews/comments in the database
- TMI configuration: `configs/teams/tmi/` (`sites_config.json`, `Keywords.json`, `negative_keywords.json`)
- Downloaded TMI tender files under `downloads/`
- TMI-specific code: the 168-hour (1 week) date window in `date_utils.py`, `cron_runner_tmi.py`, etc.

---

## How to turn the TMI portal back on

1. Open **`team_status.py`** (project root) and remove `"tmi"` from `PAUSED_TEAMS`:

   ```python
   # before
   PAUSED_TEAMS = {"tmi"}
   # after
   PAUSED_TEAMS = set()
   ```

2. Deploy and restart the app. Nothing takes effect until the server restarts:

   ```bash
   docker compose up -d --build
   ```

   (Locally: stop and re-run `launch.bat` / `uvicorn api:app`.)

3. Check it worked:
   - The server log shows **"Scheduler started — CNK job at 07:00 IST & TMI job at 21:00 IST"**.
   - `superadmin_tmi` can log in and sees the TMI dashboard.
   - If the restart happens after 9 PM IST, a TMI catch-up run starts right away. That's expected.

4. Before the TMI team relies on it again, it's worth a quick sanity pass:
   - Review `configs/teams/tmi/Keywords.json` and `sites_config.json`. Sites may have changed while paused.
   - Trigger one manual run from the TAiQ page and read the log for errors.
   - Pick up the pending request below.

That's it. **The whole pause lives in `team_status.py`.** The places that read it:

| File | What it blocks |
|---|---|
| `auth.py` → `decode_token()` | existing TMI sessions (all API routes + scrape WebSocket) |
| `routers/auth_router.py` → `login()` | new TMI logins |
| `cron_runner.py` → `run_daily_job()` | every TMI run (scheduled, catch-up, manual, `cron_runner_tmi.py`) |
| `cron_runner.py` → `init_scheduler()` / `_check_and_fire_if_needed()` | registering the 9 PM job and the catch-up |

The same switch works for any team: add its slug to `PAUSED_TEAMS`.

---

## Pending request from the TMI team

_(Fill in what the TMI team asked for so it isn't lost.)_

- **What they asked for:**
- **Who asked / when:**
- **Notes:**
