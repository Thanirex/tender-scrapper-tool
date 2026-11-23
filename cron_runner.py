import os
import re
import sys
import json
import time
import logging
import threading
from datetime import datetime
from pathlib import Path

from date_utils import now_ist_naive
from team_status import is_team_paused

logger = logging.getLogger("taiq.cron")

_scheduler      = None
_db             = None
_stop_event     = threading.Event()
# Short-held guard around claiming/releasing ownership of the run state below.
# It is never held while a scrape runs, so a wedged worker cannot block it.
_claim_lock     = threading.Lock()
_current_run_id: "int | None" = None
_current_team_id: "str | None" = None
_current_run_date: "str | None" = None   # IST date (YYYY-MM-DD) the active run belongs to
_stats          = None   # run_stats.RunStatsCollector for the active run
_progress: "dict | None" = None   # resume bookkeeping for the active run (cron_runs.progress_json)

# ── Stall watchdog ─────────────────────────────────────────────────────────
# A scrape can wedge inside a third-party call (a pathological PDF, a hung
# browser, an unresponsive host). Python cannot kill the thread doing it, so
# the run is given a generation number. A background ticker checks every
# minute; if the run stops reporting progress for _STALL_SECONDS it is
# declared dead and recovered:
#
#   • In Docker (the production server) the process exits on purpose and
#     Docker's restart policy brings the container back with a clean browser
#     and freed memory. On startup the interrupted run is resumed from the
#     next site, skipping the one that hung.
#   • Elsewhere (local dev) nothing would restart the process, so the slot is
#     released and the run resumes in a new thread instead.
#
# A run is resumed at most _MAX_RESUMES times so one bad site cannot loop.
# Any output a zombie thread produces later is dropped because its generation
# no longer matches (see _check_stop and _RunScopedDB).
_run_generation  = 0
_active_gen: "int | None" = None      # generation that currently owns run state
_last_progress   = 0.0                # time.monotonic() of the last log line
_STALL_SECONDS   = 30 * 60
_WATCHDOG_TICK   = 60                 # seconds between background stall checks
_MAX_RESUMES     = 3
_RESTART_EXIT_CODE = 75               # distinct code so a stall restart is recognisable in `docker ps -a`
_thread_state    = threading.local()  # carries the owning generation per thread
_in_server       = False              # True once init_scheduler() ran (API process, not cron_runner_tmi.py)

# ── Per-run log buffer ─────────────────────────────────────────────────────
_log_buffer:    list = []
_log_file_path: "str | None" = None
_log_lock       = threading.Lock()   # protects buffer writes


class _StopRequested(BaseException):
    """Raised to abort a running cron job.

    Inherits BaseException (not Exception) on purpose: every scraper agent
    wraps its work in broad `except Exception` blocks, which would otherwise
    swallow the stop signal and keep the run going until the site finished.
    """


def request_stop():
    _stop_event.set()
    _write_log("⏹ Stop requested via dashboard")


def _check_stop():
    if _stop_event.is_set():
        raise _StopRequested("Stop requested by superadmin")
    # A run the watchdog already gave up on must not carry on scraping if its
    # blocked call ever returns — a newer run may own the state by now.
    gen = getattr(_thread_state, "gen", None)
    if gen is not None and gen != _active_gen:
        raise _StopRequested("Run was declared stalled — standing down")


class _RunScopedDB:
    """The DB handle one run uses. Every call is refused (by raising
    _StopRequested) once that run has lost ownership to the watchdog, so a
    zombie thread can never save tenders or mark them as seen — which would
    hide them from the run that replaced it."""

    def __init__(self, db, gen: int):
        self._db  = db
        self._gen = gen

    def __getattr__(self, name):
        attr = getattr(self._db, name)
        if not callable(attr):
            return attr

        def guarded(*args, **kwargs):
            if self._gen != _active_gen:
                raise _StopRequested("Run was declared stalled — standing down")
            return attr(*args, **kwargs)
        return guarded


def _load_progress(row: "dict | None") -> dict:
    """Parse cron_runs.progress_json into a complete progress dict."""
    progress: dict = {}
    try:
        progress = json.loads((row or {}).get("progress_json") or "{}") or {}
    except Exception:
        progress = {}
    progress.setdefault("done_sites", [])
    progress.setdefault("hung_sites", [])
    progress.setdefault("current_site", "")
    progress.setdefault("resumes", 0)
    return progress


def _self_restart_enabled() -> bool:
    """Whether a stall may exit the process for Docker to restart.

    Only in the API server (a standalone cron_runner_tmi.py process has
    nothing to restart it). TAIQ_SELF_RESTART=1/0 forces it; otherwise it is
    on inside a Docker container and off everywhere else.
    """
    if not _in_server:
        return False
    flag = os.getenv("TAIQ_SELF_RESTART", "").strip().lower()
    if flag in ("1", "true", "yes", "on"):
        return True
    if flag in ("0", "false", "no", "off"):
        return False
    return Path("/.dockerenv").exists()


def _restart_process(reason: str):
    """Exit now so Docker's restart policy starts a fresh container.

    os._exit is deliberate: a graceful shutdown would wait on the very thread
    that is wedged. Everything worth keeping is already in the DB / log file.
    """
    logger.critical(
        f"[TAiQ Cron] {reason} — exiting (code {_RESTART_EXIT_CODE}) so Docker "
        f"restarts the container with a clean slate."
    )
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.flush()
        except Exception:
            pass
    for handler in logging.getLogger().handlers:
        try:
            handler.flush()
        except Exception:
            pass
    os._exit(_RESTART_EXIT_CODE)


def _append_log_file(path: "str | None", msg: str):
    """Write one line straight to a run's log file (used off the run thread)."""
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"[{now_ist_naive().strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass


def _release_slot():
    """Drop ownership of the run state. Caller must hold _claim_lock."""
    global _current_run_id, _current_team_id, _current_run_date, _stats, _progress, _active_gen
    # Clearing the generation is what makes the old thread's writes bounce.
    _active_gen        = None
    _current_run_id    = None
    _current_team_id   = None
    _current_run_date  = None
    _stats             = None
    _progress          = None


def _reap_if_stalled() -> "int | None":
    """Recover the active run if it has gone silent for _STALL_SECONDS.

    Returns the recovered run id, or None. Safe to call from any thread: it
    only touches bookkeeping, never the wedged worker. In Docker it does not
    return — the process exits and the run resumes after the restart.
    """
    with _claim_lock:
        if _active_gen is None or _current_run_id is None:
            return None
        idle = time.monotonic() - _last_progress
        if idle < _STALL_SECONDS:
            return None
        stuck_id, stuck_team = _current_run_id, _current_team_id
        log_path = _log_file_path
        _release_slot()

    mins = int(idle // 60)
    try:
        row = _db.get_cron_run(stuck_id)
    except Exception as e:
        logger.error(f"[TAiQ Cron] Could not read stalled run #{stuck_id}: {e}")
        row = None
    progress   = _load_progress(row)
    stuck_site = progress.get("current_site") or ""
    if stuck_site and stuck_site not in progress["hung_sites"]:
        progress["hung_sites"].append(stuck_site)
    progress["current_site"] = ""
    site_label = stuck_site.upper() or "the run"
    give_up    = progress["resumes"] >= _MAX_RESUMES

    if give_up:
        note = (f"⏱ {site_label} produced no output for {mins} minutes and the run has already "
                f"been resumed {progress['resumes']} time(s) — giving up on today's run.")
        update = dict(status="failed",
                      finished_at=now_ist_naive().isoformat(timespec="seconds"),
                      current_keyword="",
                      error_msg=f"Stalled on {site_label} after {progress['resumes']} resume(s) — gave up.")
    else:
        note = (f"⏱ {site_label} produced no output for {mins} minutes — restarting and "
                f"resuming the run from the next site.")
        update = dict(current_keyword="")   # stays 'running' so it is resumed
    update["progress_json"] = json.dumps(progress)

    logger.error(f"[TAiQ Cron] Run #{stuck_id} ({stuck_team}): {note}")
    _append_log_file(log_path, note)
    try:
        _db.update_cron_run(stuck_id, **update)
    except Exception as e:
        logger.error(f"[TAiQ Cron] Could not record stall of run #{stuck_id}: {e}")

    if _self_restart_enabled():
        _restart_process(f"Run #{stuck_id} stalled on {site_label}")
    elif not give_up:
        threading.Thread(
            target=run_daily_job, args=(stuck_team or "cnk",),
            kwargs={"resume_run_id": stuck_id},
            daemon=True, name=f"taiq-resume-{stuck_team}",
        ).start()
    return stuck_id


def _supersede_previous_day_run(team_id: str):
    """Before a scheduled run: if a run from an earlier day still owns the
    slot, it is stuck in a way the stall watchdog cannot see (e.g. looping
    while still logging). Close it so today's run happens regardless."""
    today = now_ist_naive().strftime("%Y-%m-%d")
    with _claim_lock:
        if _active_gen is None or _current_run_id is None:
            return
        if not _current_run_date or _current_run_date >= today:
            return
        old_id, old_team, old_date = _current_run_id, _current_team_id, _current_run_date
        log_path = _log_file_path
        _release_slot()

    note = (f"⏱ Run from {old_date} was still going when the {today} run for "
            f"{team_id.upper()} was due — closing it so the new day's run can start.")
    logger.error(f"[TAiQ Cron] Run #{old_id} ({old_team}): {note}")
    _append_log_file(log_path, note)
    try:
        _db.update_cron_run(
            old_id, status="failed",
            finished_at=now_ist_naive().isoformat(timespec="seconds"),
            current_keyword="",
            error_msg=f"Superseded — still running when the {today} run was due.",
        )
    except Exception as e:
        logger.error(f"[TAiQ Cron] Could not close superseded run #{old_id}: {e}")

    if _self_restart_enabled():
        # The startup catch-up fires today's run once the container is back.
        _restart_process(f"Run #{old_id} from {old_date} superseded by today's run")


def _scheduled_run(team_id: str):
    """APScheduler entry point.

    Returns immediately and runs the scrape in its own thread. APScheduler
    refuses to start a job while a previous call of it is still executing
    (max_instances=1), so a wedged run used to make every later 7 AM run
    silently skip until the container was restarted.
    """
    _supersede_previous_day_run(team_id)
    threading.Thread(
        target=run_daily_job, args=(team_id,), daemon=True,
        name=f"taiq-scheduled-{team_id}",
    ).start()


def _watchdog_loop():
    """Check for a stalled run every minute, whether or not anyone is
    looking at the dashboard."""
    while True:
        time.sleep(_WATCHDOG_TICK)
        try:
            _reap_if_stalled()
        except Exception as e:
            logger.error(f"[TAiQ Cron] Watchdog check failed: {e}")


def get_current_run_id() -> "int | None":
    # Checked on the API's "is a run in progress?" path, so a stalled run
    # heals itself the next time anyone looks instead of blocking every
    # future run until the container is rebuilt.
    _reap_if_stalled()
    return _current_run_id


def get_current_team_id() -> "str | None":
    return _current_team_id


def get_log_buffer() -> list:
    with _log_lock:
        return list(_log_buffer)


def _write_log(msg: str):
    """Append a timestamped line to the in-memory buffer AND the run log file."""
    global _log_buffer, _last_progress

    # A run that was declared stalled may still be alive inside a blocked
    # call. If it wakes up it must not write into the log, buffer or stats of
    # whichever run owns them now.
    gen = getattr(_thread_state, "gen", None)
    if gen is not None and gen != _active_gen:
        return

    # Any log line counts as progress for the stall watchdog.
    _last_progress = time.monotonic()

    ts   = now_ist_naive().strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    with _log_lock:
        _log_buffer.append(line)
        # Generous cap — a full daily run must fit so the dashboard shows
        # the complete log, not just the tail. (Memory safety net only.)
        if len(_log_buffer) > 100_000:
            _log_buffer = _log_buffer[-100_000:]
    if _log_file_path:
        try:
            with open(_log_file_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass
    # Every log line also feeds the run report card (see run_stats.py)
    if _stats is not None:
        try:
            _stats.feed(msg)
        except Exception:
            pass


def _reset_log(run_id: int):
    """Clear the buffer and open a fresh log file for this run."""
    global _log_buffer, _log_file_path
    from paths import CRON_LOGS_DIR
    with _log_lock:
        _log_buffer = []
    log_path       = CRON_LOGS_DIR / f"run_{run_id}.log"
    _log_file_path = str(log_path)
    # Truncate any previous file with the same run_id
    try:
        log_path.open("w").close()
    except Exception:
        pass


_LOG_TS_PREFIX = re.compile(r"^\[\d{2}:\d{2}:\d{2}\] ")


def _resume_log(run_id: int, log_file: "str | None") -> list:
    """Re-open an interrupted run's log WITHOUT truncating it.

    Loads its lines back into the live buffer so the dashboard still shows
    the whole day, and returns the bare messages so the report card can be
    rebuilt from them (run_stats is driven purely by log lines).
    """
    global _log_buffer, _log_file_path
    from paths import CRON_LOGS_DIR
    log_path = Path(log_file) if log_file else CRON_LOGS_DIR / f"run_{run_id}.log"
    lines: list = []
    try:
        if log_path.exists():
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        lines = []
    with _log_lock:
        _log_buffer = list(lines)
    _log_file_path = str(log_path)
    return [_LOG_TS_PREFIX.sub("", ln) for ln in lines]


def _clear_log():
    global _log_file_path
    _log_file_path = None


def _finalize_stats() -> "str | None":
    """Write the end-of-run report card into the log; return its JSON."""
    if _stats is None:
        return None
    from run_stats import REASON_KEYS, REASON_LABELS
    data = _stats.to_dict()
    t    = data["totals"]
    _write_log("═" * 62)
    _write_log(
        f"📋 RUN REPORT — {t['sites_scanned']} site(s) scanned, "
        f"{t['listed']} result(s) seen, {t['saved']} saved, "
        f"{t['rejected_total']} rejected"
    )
    for reason in REASON_KEYS:
        n = t["rejected"].get(reason, 0)
        if n:
            _write_log(f"   • {n} {REASON_LABELS[reason]}")
    for site, s in data["sites"].items():
        _write_log(
            f"   🔎 {site.upper()}: {s['listed']} seen → {s['saved']} saved, "
            f"{s['rejected_total']} rejected"
        )
    # Watchdog recoveries. These lines deliberately avoid the run_stats
    # marker emoji (⏭ ⏩ 🚫 📅 ⚠️ ❌) so they are never counted as rejections.
    hung = (_progress or {}).get("hung_sites") or []
    if hung:
        _write_log(f"   ⏱ Skipped after they stopped responding: {', '.join(s.upper() for s in hung)}")
    resumes = (_progress or {}).get("resumes", 0)
    if resumes:
        _write_log(f"   🔁 Resumed {resumes} time(s) after a server restart")
    _write_log("═" * 62)
    return json.dumps(data)


# ── Keyword loading ────────────────────────────────────────────────────────

def _load_all_keywords(team_id: str = "cnk") -> list:
    from paths import APP_DIR
    kw_file = APP_DIR / "configs" / "teams" / team_id / "Keywords.json"
    if not kw_file.exists():
        kw_file = APP_DIR / "Keywords.json"
    try:
        with open(kw_file, "r") as f:
            kw_data = json.load(f)
        seen: set = set()
        keywords: list = []
        for cat_kws in kw_data.values():
            for kw in cat_kws:
                if kw.lower() not in seen:
                    seen.add(kw.lower())
                    keywords.append(kw)
        return keywords
    except Exception as e:
        logger.error(f"[TAiQ Cron] Failed to load keywords for {team_id}: {e}")
        return []


# ── Main job ───────────────────────────────────────────────────────────────

def run_daily_job(team_id: str = "cnk", resume_run_id: "int | None" = None):
    """Daily TAiQ cron — scrapes ALL configured sites for all keywords for a given team.

    resume_run_id continues an interrupted run (same run id, same log) from
    the sites it had not finished, instead of starting a new one.
    """
    global _run_generation, _active_gen, _last_progress

    # Every route into a run (scheduler, catch-up, manual trigger,
    # cron_runner_tmi.py) lands here, so a paused portal is stopped once.
    if is_team_paused(team_id):
        logger.info(f"[TAiQ Cron] Run for team {team_id} skipped — portal is paused (team_status.py).")
        return

    # Only one run may own the module-level state (_current_run_id, _stats,
    # _log_buffer, _log_file_path, _stop_event) at a time.  Without this,
    # a second run entering here calls _stop_event.clear() and _reset_log(),
    # which truncates the first run's log file and steals its stats collector.
    #
    # Ownership is a generation number rather than a held lock: a lock owned by
    # a wedged worker could never be reclaimed, which is what used to force a
    # container rebuild. Stalls are detected by the background watchdog
    # (_watchdog_loop), not here — recovering from inside this call could start
    # a resume thread that races this one for the slot.
    with _claim_lock:
        if _active_gen is not None:
            logger.warning(
                f"[TAiQ Cron] Run for team {team_id} skipped — "
                f"run #{_current_run_id} ({_current_team_id}) is still in progress."
            )
            return
        _run_generation += 1
        gen = _run_generation
        _active_gen   = gen
        _last_progress = time.monotonic()

    _thread_state.gen = gen
    try:
        _run_daily_job_locked(team_id, gen, resume_run_id)
    except _StopRequested:
        # Only reachable by a zombie: a live run handles its own stop inside
        # _run_daily_job_locked. Its final status write was refused because
        # the watchdog had already recovered the run.
        logger.warning(
            f"[TAiQ Cron] A stalled {team_id} run woke up after being recovered — "
            f"it stood down without saving anything."
        )
    finally:
        with _claim_lock:
            # Only release if this run still owns the slot — if the watchdog
            # already reaped it, a newer run may hold it now.
            if _active_gen == gen:
                _active_gen = None
        _thread_state.gen = None


def _run_daily_job_locked(team_id: str, gen: int, resume_run_id: "int | None" = None):
    """Body of run_daily_job — callers must own the run slot (see _active_gen)."""
    global _current_run_id, _current_team_id, _current_run_date, _stats, _progress

    # Deliberately shadows the module-level _db for this function and every
    # callback defined in it: all of this run's DB calls go through a handle
    # that refuses to write once the watchdog has recovered the run.
    _db = _RunScopedDB(globals()["_db"], gen)

    from paths import DOWNLOADS_DIR, APP_DIR
    from db import CronDBProxy
    from agents.ungm_scraper_agent import UNGMScraperAgent
    from agents.scraper_agent import ScraperAgent
    from agents.file_reader import read_file
    from agents.summarizer_agent import SummarizerAgent
    from agents.excel_writer import write_level1_report

    _stop_event.clear()

    keywords = _load_all_keywords(team_id)
    if not keywords:
        logger.error(f"[TAiQ Cron] No keywords found for team {team_id} — job aborted.")
        return

    # Discover the sites to scrape from the team's sites_config.json
    standard_sites: list = []
    sites_cfg: dict = {}
    cfg_file = APP_DIR / "configs" / "teams" / team_id / "sites_config.json"
    if not cfg_file.exists():
        cfg_file = APP_DIR / "sites_config.json"
    try:
        with open(cfg_file, "r") as f:
            sites_cfg = json.load(f)
        # UNGM has its own agent and its own phase, so it is never dispatched
        # through the standard-site loop.
        standard_sites = [k for k in sites_cfg if k != "ungm"]
    except Exception as cfg_err:
        logger.warning(f"[TAiQ Cron] Could not load sites_config for team {team_id}: {cfg_err}")

    # UNGM's public notices need no account: the agent reads /Public/Notice
    # anonymously, so the run no longer depends on UNGM_EMAIL/UNGM_PASSWORD.
    ungm_enabled = "ungm" in sites_cfg

    if not ungm_enabled and not standard_sites:
        logger.error(f"[TAiQ Cron] No scrapable sites for team {team_id} — job aborted.")
        return

    num_sites   = (1 if ungm_enabled else 0) + len(standard_sites)
    total_kw    = len(keywords) * num_sites
    site_labels = (["UNGM"] if ungm_enabled else []) + [s.upper() for s in standard_sites]

    from run_stats import RunStatsCollector

    if resume_run_id is not None:
        # ── Resume an interrupted run (same id, same log, same date page) ──────
        row = _db.get_cron_run(resume_run_id)
        if not row or row.get("status") != "running":
            logger.warning(
                f"[TAiQ Cron] Resume of run #{resume_run_id} skipped — it is no longer running."
            )
            return
        run_id           = row["id"]
        progress         = _load_progress(row)
        progress["resumes"] += 1
        # A site still marked current was cut off by a plain restart (deploy,
        # crash) rather than a stall, so it is scanned again from the start.
        progress["current_site"] = ""
        _current_run_id   = run_id
        _current_team_id  = team_id
        _current_run_date = row.get("run_date") or now_ist_naive().strftime("%Y-%m-%d")
        _progress         = progress

        # The report card is built from log lines, so replaying the existing
        # log rebuilds it exactly as it stood before the restart.
        old_msgs = _resume_log(run_id, row.get("log_file"))
        _stats   = RunStatsCollector()
        for m in old_msgs:
            try:
                _stats.feed(m)
            except Exception:
                pass

        total_tenders = int(row.get("total_tenders") or 0)
        keywords_done = len(progress["done_sites"]) * len(keywords)
        _db.update_cron_run(
            run_id, log_file=_log_file_path, total_keywords=total_kw,
            keywords_done=keywords_done, progress_json=json.dumps(progress),
        )
        skipped = progress["done_sites"] + progress["hung_sites"]
        _write_log(
            f"🔁 Run #{run_id} resumed after a server restart "
            f"(resume {progress['resumes']} of {_MAX_RESUMES})"
            + (f" — not scanning again: {', '.join(s.upper() for s in skipped)}" if skipped else "")
        )
        logger.info(f"[TAiQ Cron] Run #{run_id} ({team_id}) resumed — resume {progress['resumes']}")
    else:
        run_id            = _db.create_cron_run(total_keywords=total_kw, team_id=team_id)
        _current_run_id   = run_id
        _current_team_id  = team_id
        _current_run_date = now_ist_naive().strftime("%Y-%m-%d")
        _progress         = _load_progress(None)
        _stats            = RunStatsCollector()

        _reset_log(run_id)
        _db.update_cron_run(run_id, log_file=_log_file_path,
                            progress_json=json.dumps(_progress))

        _write_log(
            f"Run #{run_id} started ({team_id.upper()}) — {len(keywords)} keywords × {num_sites} site(s): "
            f"{', '.join(site_labels)}"
        )
        logger.info(f"[TAiQ Cron] Run #{run_id} ({team_id}) started — {total_kw} keyword/site combinations")

        total_tenders = 0
        keywords_done = 0

    progress = _progress

    def _site_begin(site_key: str):
        # Persisted before scraping so the watchdog knows which site hung.
        progress["current_site"] = site_key
        _db.update_cron_run(run_id, progress_json=json.dumps(progress))

    def _site_end(site_key: str):
        if site_key not in progress["done_sites"]:
            progress["done_sites"].append(site_key)
        progress["current_site"] = ""
        _db.update_cron_run(run_id, progress_json=json.dumps(progress))

    def _already_handled(site_key: str) -> bool:
        """On a resumed run: True for a site not to scan again. Finished sites
        are already listed in the resume line, so only a hung one is logged.
        Log lines here must avoid the run_stats marker emoji (⏭ ⏩ 🚫 ⚠️ ❌)."""
        if site_key in progress["done_sites"]:
            return True
        if site_key in progress["hung_sites"]:
            _write_log(f"⏱ {site_key.upper()} skipped — it stopped responding earlier in this run")
            return True
        return False

    proxy      = CronDBProxy(_db, run_id, team_id=team_id)
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / "cron" / f"cron_{team_id}_{timestamp}"

    # ── Shared log callback (used by both UNGM and standard agents) ────────────
    def _progress_log(msg: str):
        nonlocal keywords_done
        # Agents emit a log line every few seconds, so raising here aborts a
        # running scrape mid-website instead of waiting for all its keywords.
        _check_stop()
        logger.info(f"[TAiQ Cron] {msg}")
        _write_log(msg)
        # UNGM agent emits this pattern — update current_keyword + keywords_done
        if "▶️ Keyword:" in msg:
            m = re.search(r"Keyword:\s*'(.+?)'", msg)
            if m:
                _db.update_cron_run(run_id, current_keyword=f"UNGM: {m.group(1)}")
        elif "tenders processed for" in msg:
            keywords_done += 1
            _db.update_cron_run(run_id, keywords_done=keywords_done)

    _driver_hint_shown = {"done": False}

    def _run_step_safely(label: str, fn):
        """Run one site/keyword scrape step. A crash in one site must not
        abort the whole daily run — log it and move on to the next step.
        (_StopRequested is a BaseException so it still passes through.)"""
        try:
            fn()
        except Exception as step_err:
            msg = f"{type(step_err).__name__}: {step_err}"
            _write_log(f"  ❌ {label} scrape error: {msg} — continuing with next step")
            logger.error(f"[TAiQ Cron] {label} scrape error: {msg}")
            # Playwright's driver process started and died before it was ready.
            # Playwright then fails on its own missing attribute, which hides
            # the cause — say it plainly, once per run.
            if (isinstance(step_err, AttributeError) and "PlaywrightContextManager" in msg
                    and not _driver_hint_shown["done"]):
                _driver_hint_shown["done"] = True
                hint = ("Playwright's browser driver died on startup — the server is "
                        "most likely out of memory or process slots, so no browser-based "
                        "site can run until the container is restarted.")
                _write_log(f"   ↳ {hint}")
                logger.error(f"[TAiQ Cron] {hint}")

    try:
        # ── Phase 1: UNGM ──────────────────────────────────────────────────────
        ungm_run_dir = run_dir / "ungm"
        # scan_ungm, not ungm_enabled, decides the scan: ungm_enabled also
        # fixes the phase numbering of every later site.
        scan_ungm = ungm_enabled and not _already_handled("ungm")
        if scan_ungm:
            _write_log(f"🌐 Phase 1 / {num_sites} — UNGM ({len(keywords)} keywords)")
        elif not ungm_enabled:
            _write_log("⏭️ Skipping UNGM — not in this team's sites_config.json.")

        def on_ungm_tender_ready(res: dict):
            nonlocal total_tenders
            _check_stop()

            title = res["title"]
            _write_log(f"  📊 Summarising: {title[:70]}")

            text_parts: list = []
            verified = res.get("verified", {})
            if verified:
                lines = ["=== VERIFIED FIELDS ==="]
                for k, v in verified.items():
                    lines.append(f"{k}: {v}")
                text_parts.append("\n".join(lines))
            page_text = res.get("page_text", "").strip()
            if page_text:
                text_parts.append(f"=== UNGM NOTICE PAGE TEXT ===\n{page_text}")
            else:
                _write_log(f"  ⚠️ No page text for: {title[:55]}")

            _MAX_CHARS_PER_FILE = 25_000
            _MAX_COMBINED_CHARS = 120_000

            files_list = res.get("files", [])
            _write_log(f"  📂 Reading {len(files_list)} file(s) for summarization...")
            for fpath in files_list:
                try:
                    file_text = read_file(fpath)
                    if file_text and file_text.strip():
                        fname = os.path.basename(fpath)
                        truncated = file_text[:_MAX_CHARS_PER_FILE]
                        text_parts.append(
                            f"=== ATTACHED DOCUMENT: {fname} ===\n{truncated.strip()}"
                        )
                    else:
                        _write_log(f"  ⚠️ Empty/unreadable: {os.path.basename(fpath)}")
                except Exception as fe:
                    _write_log(f"  ⚠️ read_file error on {os.path.basename(fpath)}: {fe}")

            combined = "\n\n".join(text_parts)
            _write_log(
                f"  📝 Combined content: {len(combined):,} chars "
                f"across {len(text_parts)} section(s)"
            )
            if not combined.strip():
                _write_log(
                    f"  ❌ Nothing to summarize — page text empty and no readable files. "
                    f"verified={bool(verified)}"
                )

            fields = summarizer.summarize_level1(
                combined, log_callback=_write_log, max_chars=_MAX_COMBINED_CHARS,
                should_stop=_stop_event.is_set,
            )
            if not fields:
                _write_log(f"  ⚠️ Summarizer returned no fields for: {title[:55]}")

            tender_dir = Path(res.get("tender_dir", str(ungm_run_dir)))
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
            excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

            record = {
                "keyword":    res["keyword"],
                "title":      title,
                "url":        res["url"],
                "site":       "ungm",
                "fields":     fields,
                "files":      res.get("files", []),
                "tender_dir": res.get("tender_dir", ""),
            }
            write_level1_report([record], excel_path)

            tender_dir_rel = ""
            try:
                tender_dir_rel = str(
                    Path(res.get("tender_dir", "")).relative_to(DOWNLOADS_DIR)
                ).replace("\\", "/")
            except ValueError:
                pass

            _db.record_cron_tender(
                run_id=run_id, keyword=res["keyword"], title=title,
                url=res.get("url", ""), site="ungm",
                published_date=res.get("deadline", ""), summary=fields,
                tender_dir=tender_dir_rel, team_id=team_id,
            )
            total_tenders += 1
            _db.update_cron_run(run_id, total_tenders=total_tenders)
            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")
            _check_stop()

        if scan_ungm:
            _site_begin("ungm")
            ungm_agent = UNGMScraperAgent()
            _run_step_safely("UNGM", lambda: ungm_agent.scrape(
                keywords, str(ungm_run_dir),
                headless=True, log_callback=_progress_log,
                on_tender_ready=on_ungm_tender_ready, db=proxy,
                team_id=team_id,
            ))
            if not _stop_event.is_set():
                _site_end("ungm")

        # ── Phase 2+: Standard sites ───────────────────────────────────────────
        if standard_sites and not _stop_event.is_set():
            from agents.nasscom_scraper_agent import NasscomScraperAgent
            from agents.au_scraper_agent import AUScraperAgent
            from agents.acbf_scraper_agent import ACBFScraperAgent
            from agents.trademark_africa_scraper_agent import TradeMarkAfricaScraperAgent
            from agents.worldbank_scraper_agent import WorldBankScraperAgent
            from agents.fhi360_scraper_agent import FHI360ScraperAgent
            from agents.gatsby_africa_scraper_agent import GatsbyAfricaScraperAgent
            from agents.afrosai_scraper_agent import AfrosaiScraperAgent
            from agents.drc_scraper_agent import DRCScraperAgent
            from agents.jsi_scraper_agent import JSIScraperAgent
            from agents.chai_scraper_agent import CHAIScraperAgent
            from agents.reliefweb_scraper_agent import ReliefWebScraperAgent
            from agents.ngobox_scraper_agent import NGOBOXScraperAgent
            from agents.idi_scraper_agent import IDIScraperAgent
            from agents.lic_scraper_agent import LICScraperAgent
            from agents.isa_scraper_agent import ISAScraperAgent
            from agents.mercycorps_scraper_agent import MercyCorpsScraperAgent
            from agents.gcf_scraper_agent import (
                GreenClimateFundScraperAgent, build_combined_text as _gcf_combined_text,
                MAX_COMBINED_CHARS as _GCF_MAX_COMBINED_CHARS,
            )
            from agents.file_reader import read_file as _read_file
            std_agent          = ScraperAgent(str(cfg_file))
            nasscom_agent      = NasscomScraperAgent()
            au_agent           = AUScraperAgent()
            acbf_agent         = ACBFScraperAgent()
            tma_agent          = TradeMarkAfricaScraperAgent()
            wb_agent           = WorldBankScraperAgent()
            fhi360_agent       = FHI360ScraperAgent()
            gatsbyafrica_agent = GatsbyAfricaScraperAgent()
            afrosai_agent      = AfrosaiScraperAgent()
            drc_agent          = DRCScraperAgent()
            jsi_agent          = JSIScraperAgent()
            chai_agent         = CHAIScraperAgent()
            reliefweb_agent    = ReliefWebScraperAgent()
            ngobox_agent       = NGOBOXScraperAgent()
            idi_agent          = IDIScraperAgent()
            lic_agent          = LICScraperAgent()
            isa_agent          = ISAScraperAgent()
            mercycorps_agent   = MercyCorpsScraperAgent()
            gcf_agent          = GreenClimateFundScraperAgent()

            for phase_idx, site_key in enumerate(standard_sites, start=2 if ungm_enabled else 1):
                if _stop_event.is_set():
                    break
                if _already_handled(site_key):
                    continue

                _write_log(
                    f"🌐 Phase {phase_idx} / {num_sites} — "
                    f"{site_key.upper()} ({len(keywords)} keywords)"
                )
                _site_begin(site_key)

                scraper_type = sites_cfg.get(site_key, {}).get("scraper_type", "standard")

                for keyword in keywords:
                    if _stop_event.is_set():
                        break

                    _db.update_cron_run(
                        run_id, current_keyword=f"{site_key.upper()}: {keyword}"
                    )
                    _write_log(f"▶️ [{site_key.upper()}] Keyword: '{keyword}'")

                    if scraper_type == "nasscom":
                        # ── Nasscom: PDF-download + file-based Level 1 ─────────
                        def on_nasscom_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date="", summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: nasscom_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_nasscom_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "au":
                        # ── African Union: list-based, direct doc download ─────
                        def on_au_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("release_date", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: au_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_au_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "acbf":
                        # ── ACBF: list-based, page-content-only Level 1 ────────
                        def on_acbf_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("deadline", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: acbf_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_acbf_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "trademarkafrica":
                        # ── TradeMark Africa: REST API + PDF download ──────────
                        def on_tma_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("deadline", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: tma_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_tma_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "fhi360":
                        # ── FHI 360 Solicitations ──────────────────────────────
                        def on_fhi360_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("closing_date", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: fhi360_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_fhi360_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "gatsbyafrica":
                        # ── Gatsby Africa Tenders ──────────────────────────────
                        def on_gatsby_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date="", summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: gatsbyafrica_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_gatsby_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "afrosai":
                        # ── AFROSAI-E Tenders ──────────────────────────────────
                        def on_afrosai_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("deadline", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: afrosai_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_afrosai_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "drc":
                        # ── DRC Tenders (active + 24h published filter) ────────
                        def on_drc_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("deadline", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: drc_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_drc_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "jsi":
                        # ── JSI Solicitations ──────────────────────────────────
                        def on_jsi_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("deadline", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: jsi_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_jsi_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "chai":
                        # ── CHAI RFP listing, 24h filter, doc download ─────────
                        def on_chai_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("published", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: chai_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_chai_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "reliefweb":
                        # ── ReliefWeb Jobs (search + pagination, page-content only) ──
                        def on_reliefweb_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("published", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: reliefweb_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_reliefweb_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "ngobox":
                        # ── NGOBOX (RFPs & EOIs) ──────────────────────────────────
                        def on_ngobox_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("published", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: ngobox_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_ngobox_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "worldbank":
                        # ── World Bank Group Procurement ───────────────────────
                        def on_wb_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date="", summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: wb_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_wb_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "idi":
                        # ── IDI Tenders ─────────────────────────────────────────
                        def on_idi_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("deadline", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: idi_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_idi_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "lic":
                        # ── LIC Tenders ─────────────────────────────────────────
                        def on_lic_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("published_date") or res.get("startDate") or res.get("deadline", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: lic_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_lic_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "isa":
                        # ── ISA (International Solar Alliance) procurement ──────
                        def on_isa_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("published_date") or res.get("startDate") or res.get("deadline", ""), summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: isa_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_isa_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "mercycorps":
                        # ── Mercy Corps tenders ─────────────────────────────────
                        def on_mercycorps_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            text_parts = []
                            page_text  = res.get("page_text", "").strip()
                            if page_text:
                                text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

                            _MAX_CHARS = 25_000
                            for fpath in res.get("files", []):
                                try:
                                    file_text = _read_file(fpath)
                                    if file_text and file_text.strip():
                                        fname = os.path.basename(fpath)
                                        text_parts.append(
                                            f"=== DOCUMENT: {fname} ===\n"
                                            f"{file_text[:_MAX_CHARS].strip()}"
                                        )
                                except Exception as fe:
                                    _write_log(f"  ⚠️ read_file error: {fe}")

                            combined = "\n\n".join(text_parts)
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log,
                                                                     should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("published_date") or res.get("deadline", ""),
                                summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: mercycorps_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_mercycorps_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    elif scraper_type == "gcf":
                        # ── Green Climate Fund procurement ──────────────────────
                        def on_gcf_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            combined = _gcf_combined_text(res, _read_file, _write_log)
                            fields   = summarizer.summarize_level1(
                                combined, log_callback=_write_log,
                                max_chars=_GCF_MAX_COMBINED_CHARS,
                                should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      res.get("files", []),
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date=res.get("published_date") or res.get("deadline", ""),
                                summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: gcf_agent.search(
                            keyword,
                            output_dir=str(run_dir / site_key),
                            log_callback=_progress_log,
                            on_result_ready=on_gcf_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    else:
                        # ── Standard search-based scraper ──────────────────────
                        def on_std_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            fields = summarizer.summarize_level1(
                                res.get("content", ""), log_callback=_write_log,
                                should_stop=_stop_event.is_set)

                            tender_dir_abs = Path(res.get("tender_dir", ""))
                            safe_title     = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
                            excel_path     = str(tender_dir_abs / f"Level1_{safe_title}.xlsx")

                            record = {
                                "keyword":    _kw,
                                "title":      title,
                                "url":        res.get("url", ""),
                                "site":       _site,
                                "fields":     fields,
                                "files":      [],
                                "tender_dir": res.get("tender_dir", ""),
                            }
                            write_level1_report([record], excel_path)

                            tender_dir_rel = ""
                            try:
                                tender_dir_rel = str(
                                    tender_dir_abs.relative_to(DOWNLOADS_DIR)
                                ).replace("\\", "/")
                            except ValueError:
                                pass

                            _db.record_cron_tender(
                                run_id=run_id, keyword=_kw, title=title,
                                url=res.get("url", ""), site=_site,
                                published_date="", summary=fields,
                                tender_dir=tender_dir_rel, team_id=team_id,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: std_agent.search(
                            site_key, keyword,
                            log_callback=_progress_log,
                            on_result_ready=on_std_result,
                            db=proxy,
                            team_id=team_id,
                        ))

                    keywords_done += 1
                    _db.update_cron_run(run_id, keywords_done=keywords_done)

                if not _stop_event.is_set():
                    _site_end(site_key)

        # ── Finish ─────────────────────────────────────────────────────────────
        finished_at = now_ist_naive().isoformat(timespec="seconds")

        if _stop_event.is_set():
            _write_log(
                f"⏹ Run stopped — {keywords_done}/{total_kw} keywords, "
                f"{total_tenders} tenders saved"
            )
            _db.update_cron_run(
                run_id, status="stopped", finished_at=finished_at,
                keywords_done=keywords_done, current_keyword="",
                total_tenders=total_tenders, stats_json=_finalize_stats(),
            )
            logger.info(
                f"[TAiQ Cron] Run #{run_id} stopped — "
                f"{keywords_done}/{total_kw} keywords, {total_tenders} tenders"
            )
        else:
            _write_log(
                f"✅ Run complete — {keywords_done}/{total_kw} keywords, "
                f"{total_tenders} tenders saved"
            )
            done_kwargs = {}
            if progress["hung_sites"]:
                done_kwargs["error_msg"] = (
                    "Completed, but skipped after they stopped responding: "
                    + ", ".join(s.upper() for s in progress["hung_sites"])
                )
            _db.update_cron_run(
                run_id, status="complete", finished_at=finished_at,
                keywords_done=keywords_done, current_keyword="",
                total_tenders=total_tenders, stats_json=_finalize_stats(),
                progress_json=json.dumps(progress), **done_kwargs,
            )
            logger.info(
                f"[TAiQ Cron] Run #{run_id} complete — {total_tenders} tenders found"
            )

    except _StopRequested:
        finished_at = now_ist_naive().isoformat(timespec="seconds")
        _write_log(
            f"⏹ Run stopped mid-scrape — {keywords_done}/{total_kw} keywords, "
            f"{total_tenders} tenders saved"
        )
        _db.update_cron_run(
            run_id, status="stopped", finished_at=finished_at,
            keywords_done=keywords_done, current_keyword="",
            total_tenders=total_tenders, stats_json=_finalize_stats(),
        )
        logger.info(
            f"[TAiQ Cron] Run #{run_id} stopped mid-scrape — "
            f"{keywords_done}/{total_kw} keywords, {total_tenders} tenders"
        )

    except Exception as e:
        import traceback
        err_msg = f"{type(e).__name__}: {e}"
        _write_log(f"❌ Run FAILED: {err_msg}")
        for line in traceback.format_exc().strip().splitlines():
            if line.strip():
                _write_log(f"   {line}")
        logger.error(f"[TAiQ Cron] Run #{run_id} FAILED: {err_msg}")
        logger.error(traceback.format_exc())
        _db.update_cron_run(
            run_id, status="failed",
            finished_at=now_ist_naive().isoformat(timespec="seconds"),
            current_keyword="", error_msg=err_msg[:500],
            stats_json=_finalize_stats(),
        )

    finally:
        # If the watchdog already reaped this run, a newer run may own the
        # module state — a late-waking zombie must not clear it, and must not
        # clear a stop that was requested for someone else.
        if getattr(_thread_state, "gen", None) == _active_gen:
            _current_run_id   = None
            _current_team_id  = None
            _current_run_date = None
            _stats            = None
            _progress         = None
            _stop_event.clear()
            _clear_log()
        else:
            logger.warning(
                f"[TAiQ Cron] Run #{run_id} finished after being declared "
                f"stalled — its results were discarded."
            )


# ── Startup catch-up ───────────────────────────────────────────────────────

def _resume_interrupted_run(today_str: str) -> "int | None":
    """Continue today's run if the previous process died while it was going
    (a stall restart, a deploy, a crash). Returns the resumed run id."""
    try:
        rows = _db.get_running_cron_runs()
    except Exception as e:
        logger.error(f"[TAiQ Cron] Startup: could not look for interrupted runs: {e}")
        return None

    for row in rows:   # newest first; only one run can be active at a time
        team = row.get("team_id") or "cnk"
        if row.get("run_date") != today_str or is_team_paused(team):
            continue   # left for mark_stale_runs_failed
        progress = _load_progress(row)
        if progress["resumes"] >= _MAX_RESUMES:
            logger.error(
                f"[TAiQ Cron] Startup: run #{row['id']} ({team}) was interrupted again after "
                f"{progress['resumes']} resume(s) — giving up on it."
            )
            _append_log_file(
                row.get("log_file"),
                f"⏱ Run interrupted again after {progress['resumes']} resume(s) — giving up on today's run.",
            )
            _db.update_cron_run(
                row["id"], status="failed",
                finished_at=now_ist_naive().isoformat(timespec="seconds"),
                current_keyword="",
                error_msg=f"Interrupted again after {progress['resumes']} resume(s) — gave up.",
            )
            continue
        logger.info(f"[TAiQ Cron] Startup: resuming interrupted run #{row['id']} ({team})")
        threading.Thread(
            target=run_daily_job, args=(team,), kwargs={"resume_run_id": row["id"]},
            daemon=True, name=f"taiq-resume-{team}",
        ).start()
        return row["id"]
    return None


def _check_and_fire_if_needed():
    import pytz
    IST       = pytz.timezone("Asia/Kolkata")
    now_ist   = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")

    resumed_id  = _resume_interrupted_run(today_str)
    stale_count = _db.mark_stale_runs_failed(keep_id=resumed_id)
    if stale_count:
        logger.info(f"[TAiQ Cron] Startup: marked {stale_count} stale run(s) as failed")

    # CNK catch-up check: past 7:00 AM IST and CNK has not run today
    if now_ist.hour >= 7:
        cnk_run = _db.get_cron_run_by_date(today_str, team_id="cnk")
        if not cnk_run and _current_run_id is None:
            logger.info("[TAiQ Cron] Catch-up: past 7am IST with no CNK run today — firing CNK job")
            threading.Thread(
                target=run_daily_job, args=("cnk",), daemon=True, name="taiq-catchup-cnk"
            ).start()

    # TMI catch-up check: past 9:00 PM IST (21:00) and TMI has not run today
    if now_ist.hour >= 21 and not is_team_paused("tmi"):
        tmi_run = _db.get_cron_run_by_date(today_str, team_id="tmi")
        if not tmi_run and _current_run_id is None:
            logger.info("[TAiQ Cron] Catch-up: past 9pm IST with no TMI run today — firing TMI job")
            threading.Thread(
                target=run_daily_job, args=("tmi",), daemon=True, name="taiq-catchup-tmi"
            ).start()


# ── Scheduler init ─────────────────────────────────────────────────────────

def init_scheduler(db):
    global _scheduler, _db, _in_server
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    _db = db
    _in_server = True
    IST = pytz.timezone("Asia/Kolkata")

    _scheduler = BackgroundScheduler(timezone=IST)

    # Jobs call _scheduled_run, which hands the scrape to its own thread and
    # returns at once — a wedged run can then never block the next day's job.
    # 1) CNK Daily Scrape at 07:00 AM IST
    _scheduler.add_job(
        _scheduled_run,
        trigger=CronTrigger(hour=7, minute=0, timezone=IST),
        args=["cnk"],
        id="daily_taiq_job_cnk",
        name="TAiQ CNK Daily Scrape (07:00 AM IST)",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # 2) TMI Daily Scrape at 09:00 PM IST (21:00) — not scheduled while paused
    if not is_team_paused("tmi"):
        _scheduler.add_job(
            _scheduled_run,
            trigger=CronTrigger(hour=21, minute=0, timezone=IST),
            args=["tmi"],
            id="daily_taiq_job_tmi",
            name="TAiQ TMI Daily Scrape (09:00 PM IST)",
            replace_existing=True,
            misfire_grace_time=3600,
        )

    _scheduler.start()
    if is_team_paused("tmi"):
        logger.info("[TAiQ Cron] Scheduler started — CNK job at 07:00 IST (TMI portal paused, no TMI job)")
    else:
        logger.info("[TAiQ Cron] Scheduler started — CNK job at 07:00 IST & TMI job at 21:00 IST")

    threading.Thread(
        target=_check_and_fire_if_needed, daemon=True, name="taiq-startup-check"
    ).start()
    threading.Thread(
        target=_watchdog_loop, daemon=True, name="taiq-stall-watchdog"
    ).start()
    logger.info(
        f"[TAiQ Cron] Stall watchdog on — {_STALL_SECONDS // 60} min without progress "
        f"triggers recovery ({'container restart' if _self_restart_enabled() else 'in-process resume'})"
    )


def shutdown_scheduler():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[TAiQ Cron] Scheduler stopped")
