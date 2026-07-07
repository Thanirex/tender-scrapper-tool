import os
import re
import json
import logging
import threading
from datetime import datetime
from pathlib import Path

from date_utils import now_ist_naive

logger = logging.getLogger("taiq.cron")

_scheduler      = None
_db             = None
_stop_event     = threading.Event()
_current_run_id: "int | None" = None

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


def get_current_run_id() -> "int | None":
    return _current_run_id


def get_log_buffer() -> list:
    with _log_lock:
        return list(_log_buffer)


def _write_log(msg: str):
    """Append a timestamped line to the in-memory buffer AND the run log file."""
    global _log_buffer
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


def _clear_log():
    global _log_file_path
    _log_file_path = None


# ── Keyword loading ────────────────────────────────────────────────────────

def _load_all_keywords() -> list:
    from paths import APP_DIR
    try:
        with open(APP_DIR / "Keywords.json", "r") as f:
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
        logger.error(f"[TAiQ Cron] Failed to load Keywords.json: {e}")
        return []


# ── Main job ───────────────────────────────────────────────────────────────

def run_daily_job():
    """Daily TAiQ cron — scrapes ALL configured sites for all keywords at 7am IST."""
    global _current_run_id

    from paths import DOWNLOADS_DIR, APP_DIR
    from db import CronDBProxy
    from agents.ungm_scraper_agent import UNGMScraperAgent
    from agents.scraper_agent import ScraperAgent
    from agents.file_reader import read_file
    from agents.summarizer_agent import SummarizerAgent
    from agents.excel_writer import write_level1_report

    _stop_event.clear()

    email    = os.getenv("UNGM_EMAIL", "").strip()
    password = os.getenv("UNGM_PASSWORD", "")

    if not email or not password:
        logger.error("[TAiQ Cron] UNGM_EMAIL or UNGM_PASSWORD not set — job aborted.")
        return

    keywords = _load_all_keywords()
    if not keywords:
        logger.error("[TAiQ Cron] No keywords found — job aborted.")
        return

    # Discover standard (non-auth) sites from sites_config.json
    standard_sites: list = []
    try:
        with open(APP_DIR / "sites_config.json", "r") as f:
            sites_cfg = json.load(f)
        standard_sites = [k for k, v in sites_cfg.items() if not v.get("requires_auth")]
    except Exception as cfg_err:
        logger.warning(f"[TAiQ Cron] Could not load sites_config.json: {cfg_err}")

    num_sites   = 1 + len(standard_sites)          # 1 = UNGM + rest
    total_kw    = len(keywords) * num_sites
    site_labels = ["UNGM"] + [s.upper() for s in standard_sites]

    run_id          = _db.create_cron_run(total_keywords=total_kw)
    _current_run_id = run_id

    _reset_log(run_id)
    _db.update_cron_run(run_id, log_file=_log_file_path)

    _write_log(
        f"Run #{run_id} started — {len(keywords)} keywords × {num_sites} site(s): "
        f"{', '.join(site_labels)}"
    )
    logger.info(f"[TAiQ Cron] Run #{run_id} started — {total_kw} keyword/site combinations")

    proxy      = CronDBProxy(_db, run_id)
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / "cron" / f"cron_{timestamp}"

    total_tenders = 0
    keywords_done = 0

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

    try:
        # ── Phase 1: UNGM ──────────────────────────────────────────────────────
        ungm_run_dir = run_dir / "ungm"
        _write_log(f"🌐 Phase 1 / {num_sites} — UNGM ({len(keywords)} keywords)")

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
                combined, log_callback=_write_log, max_chars=_MAX_COMBINED_CHARS
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
                tender_dir=tender_dir_rel,
            )
            total_tenders += 1
            _db.update_cron_run(run_id, total_tenders=total_tenders)
            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")
            _check_stop()

        ungm_agent = UNGMScraperAgent()
        _run_step_safely("UNGM", lambda: ungm_agent.scrape(
            email, password, keywords, str(ungm_run_dir),
            headless=True, log_callback=_progress_log,
            on_tender_ready=on_ungm_tender_ready, db=proxy,
        ))

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
            from agents.file_reader import read_file as _read_file
            std_agent          = ScraperAgent(str(APP_DIR / "sites_config.json"))
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

            for phase_idx, site_key in enumerate(standard_sites, start=2):
                if _stop_event.is_set():
                    break

                _write_log(
                    f"🌐 Phase {phase_idx} / {num_sites} — "
                    f"{site_key.upper()} ({len(keywords)} keywords)"
                )

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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                            fields   = summarizer.summarize_level1(combined, log_callback=_write_log)

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
                                tender_dir=tender_dir_rel,
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
                        ))

                    else:
                        # ── Standard search-based scraper ──────────────────────
                        def on_std_result(res, _kw=keyword, _site=site_key):
                            nonlocal total_tenders
                            _check_stop()

                            title = res.get("title", "Unknown")
                            _write_log(f"  📊 Summarising: {title[:70]}")

                            fields = summarizer.summarize_level1(res.get("content", ""))

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
                                tender_dir=tender_dir_rel,
                            )
                            total_tenders += 1
                            _db.update_cron_run(run_id, total_tenders=total_tenders)
                            _write_log(f"  ✅ Saved tender #{total_tenders}: {title[:60]}")

                        _run_step_safely(site_key.upper(), lambda: std_agent.search(
                            site_key, keyword,
                            log_callback=_progress_log,
                            on_result_ready=on_std_result,
                            db=proxy,
                        ))

                    keywords_done += 1
                    _db.update_cron_run(run_id, keywords_done=keywords_done)

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
                total_tenders=total_tenders,
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
            _db.update_cron_run(
                run_id, status="complete", finished_at=finished_at,
                keywords_done=keywords_done, current_keyword="",
                total_tenders=total_tenders,
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
            total_tenders=total_tenders,
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
        )

    finally:
        _current_run_id = None
        _stop_event.clear()
        _clear_log()


# ── Startup catch-up ───────────────────────────────────────────────────────

def _check_and_fire_if_needed():
    import pytz
    IST       = pytz.timezone("Asia/Kolkata")
    now_ist   = datetime.now(IST)
    today_str = now_ist.strftime("%Y-%m-%d")

    stale_count = _db.mark_stale_runs_failed()
    if stale_count:
        logger.info(f"[TAiQ Cron] Startup: marked {stale_count} stale run(s) as failed")

    if now_ist.hour >= 7:
        existing = _db.get_cron_run_by_date(today_str)
        if not existing:
            logger.info(
                "[TAiQ Cron] Catch-up: past 7am IST with no run today — firing now"
            )
            threading.Thread(
                target=run_daily_job, daemon=True, name="taiq-catchup"
            ).start()
        else:
            logger.info(
                f"[TAiQ Cron] Startup: today's run exists "
                f"(id={existing['id']}, status={existing['status']}) — skipping"
            )
    else:
        logger.info(
            f"[TAiQ Cron] Startup: before 7am IST ({now_ist.strftime('%H:%M')}) — "
            "no catch-up needed"
        )


# ── Scheduler init ─────────────────────────────────────────────────────────

def init_scheduler(db):
    global _scheduler, _db
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger
    import pytz

    _db = db
    IST = pytz.timezone("Asia/Kolkata")

    _scheduler = BackgroundScheduler(timezone=IST)
    _scheduler.add_job(
        run_daily_job,
        trigger=CronTrigger(hour=7, minute=0, timezone=IST),
        id="daily_taiq_job",
        name="TAiQ Daily Scrape",
        replace_existing=True,
        misfire_grace_time=3600,
    )
    _scheduler.start()
    logger.info("[TAiQ Cron] Scheduler started — daily job fires at 07:00 IST")

    threading.Thread(
        target=_check_and_fire_if_needed, daemon=True, name="taiq-startup-check"
    ).start()


def shutdown_scheduler():
    global _scheduler
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)
        logger.info("[TAiQ Cron] Scheduler stopped")
