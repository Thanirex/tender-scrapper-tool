import os
import re
import json
import time
import asyncio
import platform
import zipfile
from pathlib import Path
from datetime import datetime
from typing import Optional

from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from dotenv import load_dotenv

from agents.scraper_agent import ScraperAgent
from agents.summarizer_agent import SummarizerAgent
from paths import APP_DIR, OUTPUTS_DIR, DOWNLOADS_DIR, DB_PATH, init as _init_paths
from db import TenderDB
from auth import decode_token, hash_password
from date_utils import now_ist_naive
from run_stats import RunStatsCollector

load_dotenv(APP_DIR / ".env")

_init_paths()
_db = TenderDB(DB_PATH)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    import cron_runner
    cron_runner.init_scheduler(_db)
    yield
    cron_runner.shutdown_scheduler()


app = FastAPI(lifespan=_lifespan)
app.state.db = _db

# ── Routers ────────────────────────────────────────────────────────────────
from routers.auth_router import router as auth_router
from routers.admin_router import router as admin_router
from routers.dashboard_router import router as dashboard_router
from routers.superadmin_router import router as superadmin_router
from routers.taiq_router import router as taiq_router
from routers.review_router import router as review_router

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(dashboard_router)
app.include_router(superadmin_router)
app.include_router(taiq_router)
app.include_router(review_router)

# ── Seed superadmin on first startup ──────────────────────────────────────

def _seed_superadmin():
    cnk_pass  = os.getenv("SUPERADMIN_CNK_PASSWORD", os.getenv("SUPERADMIN_PASSWORD", "Cnkonline@2026"))
    tmi_pass  = os.getenv("SUPERADMIN_TMI_PASSWORD", "Tmionline@2026")
    cnk_hash  = hash_password(cnk_pass)
    tmi_hash  = hash_password(tmi_pass)

    # Seed or update CNK Super Admin ('superadmin' and 'superadmin_cnk')
    for u_name, u_email in [("superadmin_cnk", "admin@cnk.local"), ("superadmin", "admin@yourorg.com")]:
        existing = _db.get_user_by_username(u_name)
        if not existing:
            _db.create_user(u_name, u_email, cnk_hash, "superadmin", team_id="cnk", team_name="CNK")
            print(f"[TAiQ] Seeded '{u_name}'")
        else:
            with _db._connect() as conn:
                conn.execute(
                    "UPDATE users SET password_hash=?, team_id='cnk', team_name='CNK' WHERE username=?",
                    (cnk_hash, u_name)
                )

    # Seed or update TMI Super Admin ('superadmin_tmi')
    existing_tmi = _db.get_user_by_username("superadmin_tmi")
    if not existing_tmi:
        _db.create_user("superadmin_tmi", "admin@tmi.local", tmi_hash, "superadmin", team_id="tmi", team_name="TMI")
        print(f"[TAiQ] Seeded 'superadmin_tmi'")
    else:
        with _db._connect() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?, team_id='tmi', team_name='TMI' WHERE username='superadmin_tmi'",
                (tmi_hash,)
            )

_seed_superadmin()

# ── Static / page routes ──────────────────────────────────────────────────

app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.mount("/assets", StaticFiles(directory=str(APP_DIR / "Assets")), name="assets")


@app.get("/")
async def read_index():
    return FileResponse(str(APP_DIR / "static" / "index.html"))


@app.get("/login")
async def read_login():
    return FileResponse(str(APP_DIR / "static" / "login.html"))


@app.get("/dashboard")
async def read_dashboard():
    return FileResponse(str(APP_DIR / "static" / "dashboard.html"))


@app.get("/users")
async def read_users():
    return FileResponse(str(APP_DIR / "static" / "users.html"))


@app.get("/audit")
async def read_audit():
    return FileResponse(str(APP_DIR / "static" / "audit.html"))


@app.get("/taiq-work")
async def read_taiq():
    return FileResponse(str(APP_DIR / "static" / "taiq.html"))


@app.get("/status")
async def read_status():
    return FileResponse(str(APP_DIR / "static" / "status.html"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/config")
async def get_config(request: Request, team_id: Optional[str] = Query(default=None)):
    if not team_id:
        auth_header = request.headers.get("authorization")
        if auth_header and auth_header.startswith("Bearer "):
            try:
                user = decode_token(auth_header.split(" ")[1])
                team_id = user.get("team_id", "cnk")
            except Exception:
                team_id = "cnk"
        else:
            team_id = "cnk"

    team_id = (team_id or "cnk").lower().strip()
    cfg_dir = APP_DIR / "configs" / "teams" / team_id
    sites_file = cfg_dir / "sites_config.json" if (cfg_dir / "sites_config.json").exists() else APP_DIR / "sites_config.json"
    kw_file = cfg_dir / "Keywords.json" if (cfg_dir / "Keywords.json").exists() else APP_DIR / "Keywords.json"

    try:
        with open(sites_file, "r") as f:
            sites = json.load(f)
    except Exception:
        sites = {}
    try:
        with open(kw_file, "r") as f:
            keywords = json.load(f)
    except Exception:
        keywords = {}
    return {"sites": list(sites.keys()), "keywords": keywords}


def _log_download(payload: dict, action: str, name: str):
    """Record who downloaded what for the audit log. Never blocks the download."""
    try:
        _db.log_activity(int(payload.get("sub", 0)), payload.get("username", "?"),
                         action, details={"name": name})
    except Exception:
        pass


@app.get("/download")
async def download_file(name: str, token: Optional[str] = Query(default=None)):
    if not token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        payload = decode_token(token)
    except Exception:
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    file_path = (OUTPUTS_DIR / name).resolve()
    if not file_path.is_relative_to(OUTPUTS_DIR.resolve()):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not file_path.exists() or not file_path.is_file():
        return JSONResponse({"error": "File not found"}, status_code=404)
    ext = file_path.suffix.lower()
    mime_map = {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".zip":  "application/zip",
    }
    _log_download(payload, "download_report", name)
    return FileResponse(
        str(file_path),
        media_type=mime_map.get(ext, "application/octet-stream"),
        filename=name,
    )


@app.get("/tender/files")
async def list_tender_files(
    dir: str,
    token: Optional[str] = Query(default=None),
):
    if not token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        decode_token(token)
    except Exception:
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    tender_dir = (DOWNLOADS_DIR / dir).resolve()
    if not tender_dir.is_relative_to(DOWNLOADS_DIR.resolve()):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not tender_dir.exists() or not tender_dir.is_dir():
        return JSONResponse({"error": "Folder not found"}, status_code=404)

    files = []
    for fpath in sorted(tender_dir.rglob("*")):
        if fpath.is_file() and fpath.name != "page_content.txt":
            rel = str(fpath.relative_to(DOWNLOADS_DIR)).replace("\\", "/")
            files.append({"name": fpath.name, "path": rel})
    return files


@app.get("/download/file")
async def download_single_file(
    path: str,
    token: Optional[str] = Query(default=None),
):
    if not token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        payload = decode_token(token)
    except Exception:
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    file_path = (DOWNLOADS_DIR / path).resolve()
    if not file_path.is_relative_to(DOWNLOADS_DIR.resolve()):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not file_path.exists() or not file_path.is_file():
        return JSONResponse({"error": "File not found"}, status_code=404)

    ext = file_path.suffix.lower()
    mime_map = {
        ".pdf":  "application/pdf",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".doc":  "application/msword",
        ".zip":  "application/zip",
        ".txt":  "text/plain",
    }
    _log_download(payload, "download_file", file_path.name)
    return FileResponse(
        str(file_path),
        media_type=mime_map.get(ext, "application/octet-stream"),
        filename=file_path.name,
    )


@app.get("/download/tender")
async def download_tender_folder(
    path: str,
    token: Optional[str] = Query(default=None),
):
    if not token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        payload = decode_token(token)
    except Exception:
        return JSONResponse({"error": "Invalid token"}, status_code=401)

    tender_dir = (DOWNLOADS_DIR / path).resolve()
    if not tender_dir.is_relative_to(DOWNLOADS_DIR.resolve()):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not tender_dir.exists() or not tender_dir.is_dir():
        return JSONResponse({"error": "Folder not found"}, status_code=404)

    safe_name = re.sub(r'[^a-zA-Z0-9_\-]', '_', tender_dir.name)[:50]
    zip_name  = f"tender_{safe_name}.zip"
    zip_path  = OUTPUTS_DIR / zip_name

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for fpath in tender_dir.rglob("*"):
            if fpath.is_file():
                zf.write(fpath, fpath.relative_to(tender_dir))

    _log_download(payload, "download_tender_zip", tender_dir.name)
    return FileResponse(str(zip_path), media_type="application/zip", filename=zip_name)


# ── Zip helper ─────────────────────────────────────────────────────────────

def _make_run_zip(dirs: list[Path], base: Path, zip_stem: str) -> Path:
    zip_path = OUTPUTS_DIR / f"{zip_stem}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in dirs:
            if not src.is_dir():
                continue
            for fpath in src.rglob("*"):
                if fpath.is_file():
                    zf.write(fpath, fpath.relative_to(base))
    return zip_path


# ── Standard scraper (ngobox / devnet) ────────────────────────────────────

def _run_standard_scrape(site_key: str, keywords: list, log_cb,
                         result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.excel_writer import write_level1_report
    cfg_dir = APP_DIR / "configs" / "teams" / team_id
    cfg_file = cfg_dir / "sites_config.json" if (cfg_dir / "sites_config.json").exists() else APP_DIR / "sites_config.json"
    agent = ScraperAgent(str(cfg_file))
    summarizer = SummarizerAgent()

    tender_dirs: list[Path] = []
    log_cb("🚀 Starting Agentic Scraper...")

    def make_callback(kw):
        def on_result_ready(res):
            title = res.get("title", "Unknown")
            log_cb(f"📊 Summarizing: {title[:55]}...")
            fields = summarizer.summarize_level1(res.get("content", ""), log_callback=log_cb)
            record = {
                "keyword": kw,
                "title": title,
                "url": res.get("url", ""),
                "site": site_key,
                "fields": fields,
                "files": [],
                "tender_dir": res.get("tender_dir", ""),
            }
            tender_dir = Path(res["tender_dir"])
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
            excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")
            write_level1_report([record], excel_path)
            tender_dirs.append(tender_dir)
            if result_cb:
                result_cb(record)
            log_cb(f"✅ Saved: {excel_path}")
        return on_result_ready

    from date_utils import get_max_age_hours
    max_age_hours = get_max_age_hours(team_id)

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(site_key, kw, log_callback=log_cb,
                     on_result_ready=make_callback(kw), db=_db,
                     team_id=team_id)

    if not tender_dirs:
        log_cb(f"ℹ️ Run finished — nothing new to save. Everything listed was either "
               f"outside the {max_age_hours}-hour window, already collected in an earlier run, "
               f"or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── Nasscom scraper (PDF-download, no search box, dedup-only) ─────────────

def _run_nasscom_scrape(site_key: str, keywords: list, log_cb,
                        result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.nasscom_scraper_agent import NasscomScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report

    agent      = NasscomScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting Nasscom Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb("ℹ️ Run finished — nothing new to save. Everything listed was either "
               "outside the 24-hour window, already collected in an earlier run, "
               "or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── TradeMark Africa scraper (WP REST API + PDF download) ─────────────────

def _run_trademarkafrica_scrape(site_key: str, keywords: list, log_cb,
                                result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.trademark_africa_scraper_agent import TradeMarkAfricaScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report

    agent      = TradeMarkAfricaScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting TradeMark Africa Procurement Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb("ℹ️ Run finished — nothing new to save. Everything listed was either "
               "outside the 24-hour window, already collected in an earlier run, "
               "or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── World Bank Group Procurement scraper ──────────────────────────────────

def _run_worldbank_scrape(site_key: str, keywords: list, log_cb,
                          result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.worldbank_scraper_agent import WorldBankScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report

    agent      = WorldBankScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting World Bank Group Procurement Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb("ℹ️ Run finished — nothing new to save. Everything listed was either "
               "outside the 24-hour window, already collected in an earlier run, "
               "or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── FHI 360 scraper (single listing page, HR-delimited blocks) ─────────────

def _run_fhi360_scrape(site_key: str, keywords: list, log_cb,
                       result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.fhi360_scraper_agent import FHI360ScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report

    agent      = FHI360ScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting FHI 360 Solicitations Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb("ℹ️ Run finished — nothing new to save. Everything listed was either "
               "outside the 24-hour window, already collected in an earlier run, "
               "or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── Gatsby Africa scraper (search + detail page + PDF download) ────────────

def _run_gatsbyafrica_scrape(site_key: str, keywords: list, log_cb,
                              result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.gatsby_africa_scraper_agent import GatsbyAfricaScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report

    agent      = GatsbyAfricaScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting Gatsby Africa Tenders Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb("ℹ️ Run finished — nothing new to save. Everything listed was either "
               "outside the 24-hour window, already collected in an earlier run, "
               "or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── JSI scraper (listing → detail → RFP download via solicitations CDN) ───

def _run_jsi_scrape(site_key: str, keywords: list, log_cb,
                    result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.jsi_scraper_agent import JSIScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report

    agent      = JSIScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting JSI Solicitations Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb("ℹ️ Run finished — nothing new to save. Everything listed was either "
               "outside the 24-hour window, already collected in an earlier run, "
               "or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── CHAI scraper (RFP listing, 24h filter, wp-content doc download) ───────

def _run_chai_scrape(site_key: str, keywords: list, log_cb,
                     result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.chai_scraper_agent import CHAIScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report
    from date_utils import get_max_age_hours
    max_age_hours = get_max_age_hours(team_id)

    agent      = CHAIScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting Clinton Health Access Initiative (CHAI) Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb(f"ℹ️ Run finished — nothing new to save. Everything listed was either "
               f"outside the {max_age_hours}-hour window, already collected in an earlier run, "
               f"or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── DRC scraper (table listing, active + 24h filter, detail page docs) ────

def _run_drc_scrape(site_key: str, keywords: list, log_cb,
                    result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.drc_scraper_agent import DRCScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report
    from date_utils import get_max_age_hours
    max_age_hours = get_max_age_hours(team_id)

    agent      = DRCScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting DRC Tenders Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb(f"ℹ️ Run finished — nothing new to save. Everything listed was either "
               f"outside the {max_age_hours}-hour window, already collected in an earlier run, "
               f"or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── AFROSAI-E scraper (listing page, direct PDF download) ─────────────────

def _run_afrosai_scrape(site_key: str, keywords: list, log_cb,
                         result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.afrosai_scraper_agent import AfrosaiScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report
    from date_utils import get_max_age_hours
    max_age_hours = get_max_age_hours(team_id)

    agent      = AfrosaiScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting AFROSAI-E Tenders Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb(f"ℹ️ Run finished — nothing new to save. Everything listed was either "
               f"outside the {max_age_hours}-hour window, already collected in an earlier run, "
               f"or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── ACBF scraper (list-based, page-content-only) ──────────────────────────

def _run_acbf_scrape(site_key: str, keywords: list, log_cb,
                     result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.acbf_scraper_agent import ACBFScraperAgent
    from agents.excel_writer import write_level1_report
    from date_utils import get_max_age_hours
    max_age_hours = get_max_age_hours(team_id)

    agent      = ACBFScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting ACBF Procurement Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        page_text = res.get("page_text", "").strip()
        combined  = f"=== PAGE CONTENT ===\n{page_text}" if page_text else ""
        fields    = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb(f"ℹ️ Run finished — nothing new to save. Everything listed was either "
               f"outside the {max_age_hours}-hour window, already collected in an earlier run, "
               f"or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── ReliefWeb Jobs scraper (search + pagination, page-content Level 1) ─────

def _run_reliefweb_scrape(site_key: str, keywords: list, log_cb,
                          result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.reliefweb_scraper_agent import ReliefWebScraperAgent
    from agents.excel_writer import write_level1_report
    from date_utils import get_max_age_hours
    max_age_hours = get_max_age_hours(team_id)

    agent      = ReliefWebScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting ReliefWeb Jobs Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        page_text = res.get("page_text", "").strip()
        combined  = f"=== PAGE CONTENT ===\n{page_text}" if page_text else ""
        fields    = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb(f"ℹ️ Run finished — nothing new to save. Everything listed was either "
               f"outside the {max_age_hours}-hour window, already collected in an earlier run, "
               f"or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── NGOBOX scraper (search + detail page + document download) ──────────────

def _run_ngobox_scrape(site_key: str, keywords: list, log_cb,
                       result_cb=None, team_id: str = "tmi") -> list[Path] | None:
    from agents.ngobox_scraper_agent import NGOBOXScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report
    from date_utils import get_max_age_hours
    max_age_hours = get_max_age_hours(team_id)

    agent      = NGOBOXScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting NGOBOX RFPs & EOIs Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        _MAX_CHARS = 25_000
        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:_MAX_CHARS].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),  # NGOBOX detail page link preserved!
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb(f"ℹ️ Run finished — nothing new to save. Everything listed was either "
               f"outside the {max_age_hours}-hour window, already collected in an earlier run, "
               f"or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── African Union scraper (list-based, direct doc download) ───────────────

def _run_au_scrape(site_key: str, keywords: list, log_cb,
                   result_cb=None, team_id: str = "cnk") -> list[Path] | None:
    from agents.au_scraper_agent import AUScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report
    from date_utils import get_max_age_hours
    max_age_hours = get_max_age_hours(team_id)

    agent      = AUScraperAgent()
    summarizer = SummarizerAgent()
    timestamp  = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir    = DOWNLOADS_DIR / site_key / timestamp
    tender_dirs: list[Path] = []

    log_cb("🚀 Starting African Union Bids Scraper...")

    def on_result_ready(res):
        title = res.get("title", "Unknown")
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
        page_text  = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== PAGE CONTENT ===\n{page_text}")

        for fpath in res.get("files", []):
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    text_parts.append(
                        f"=== DOCUMENT: {fname} ===\n{file_text[:25_000].strip()}"
                    )
            except Exception as fe:
                log_cb(f"⚠️ read_file error: {fe}")

        combined = "\n\n".join(text_parts)
        fields   = summarizer.summarize_level1(combined, log_callback=log_cb)

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")

        record = {
            "keyword":    res.get("keyword", ""),
            "title":      title,
            "url":        res.get("url", ""),
            "site":       site_key,
            "fields":     fields,
            "files":      res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        write_level1_report([record], excel_path)
        tender_dirs.append(tender_dir)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(
            kw,
            output_dir=str(run_dir),
            log_callback=log_cb,
            on_result_ready=on_result_ready,
            db=_db,
            team_id=team_id,
        )

    if not tender_dirs:
        log_cb(f"ℹ️ Run finished — nothing new to save. Everything listed was either "
               f"outside the {max_age_hours}-hour window, already collected in an earlier run, "
               f"or didn't match your keywords.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── UNGM scraper ───────────────────────────────────────────────────────────

def _run_ungm_scrape(keywords: list, credentials: dict, log_cb,
                     result_cb=None, team_id: str = "cnk") -> Path | None:
    from agents.ungm_scraper_agent import UNGMScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report

    email       = credentials.get("email", "").strip()
    password    = credentials.get("password", "")
    show_browser = credentials.get("show_browser", False)

    if show_browser and platform.system() == "Linux" and not os.getenv("DISPLAY"):
        show_browser = False
        log_cb("ℹ️ No display server — running headless.")

    if not email or not password:
        log_cb("❌ UNGM email and password are required.")
        return None

    timestamp = now_ist_naive().strftime("%Y%m%d_%H%M%S")
    run_dir = DOWNLOADS_DIR / "ungm" / timestamp
    summarizer = SummarizerAgent()
    excel_paths = []

    def on_tender_ready(res):
        title = res["title"]
        log_cb(f"📊 Summarizing: {title[:55]}...")

        text_parts = []
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
            log_cb(f"⚠️ No page text scraped for: {title[:55]}")
        # Cap each file to avoid blowing Gemini's tokens-per-minute quota.
        # UNGM can download 8+ large PDFs; without a per-file cap the combined
        # prompt can exceed 100K+ tokens and trigger RESOURCE_EXHAUSTED on every retry.
        _MAX_CHARS_PER_FILE = 25_000
        _MAX_COMBINED_CHARS = 120_000

        files_list = res.get("files", [])
        log_cb(f"   📂 Reading {len(files_list)} file(s) for summarization...")
        for fpath in files_list:
            try:
                file_text = read_file(fpath)
                if file_text and file_text.strip():
                    fname = os.path.basename(fpath)
                    truncated = file_text[:_MAX_CHARS_PER_FILE]
                    text_parts.append(f"=== ATTACHED DOCUMENT: {fname} ===\n{truncated.strip()}")
                else:
                    log_cb(f"   ⚠️ Empty/unreadable: {os.path.basename(fpath)}")
            except Exception as fe:
                log_cb(f"   ⚠️ read_file error on {os.path.basename(fpath)}: {fe}")

        combined = "\n\n".join(text_parts)
        log_cb(f"   📝 Combined content: {len(combined):,} chars across {len(text_parts)} section(s)")
        if not combined.strip():
            log_cb(f"❌ Nothing to summarize — page text empty and no readable files. verified={bool(verified)}")
        fields = summarizer.summarize_level1(combined, log_callback=log_cb,
                                             max_chars=_MAX_COMBINED_CHARS)
        if not fields:
            log_cb(f"⚠️ Summarizer returned no fields for: {title[:55]}")

        record = {
            "keyword": res["keyword"],
            "title": title,
            "url": res["url"],
            "site": "ungm",
            "fields": fields,
            "files": res.get("files", []),
            "tender_dir": res.get("tender_dir", ""),
        }
        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")
        write_level1_report([record], excel_path)
        excel_paths.append(excel_path)
        if result_cb:
            result_cb(record)
        log_cb(f"✅ Saved: {excel_path}")

    log_cb("🚀 Starting UNGM Agentic Scraper...")
    if show_browser:
        log_cb("👁️ Live browser mode enabled.")

    agent = UNGMScraperAgent()
    agent.scrape(email, password, keywords, str(run_dir),
                 headless=not show_browser, log_callback=log_cb,
                 on_tender_ready=on_tender_ready, db=_db, team_id=team_id)

    if not excel_paths:
        log_cb("⚠️ No tenders found.")
        return None

    log_cb(f"✅ Done. {len(excel_paths)} Level 1 Excel(s) saved in: {run_dir}")
    return run_dir


# ── WebSocket scrape endpoint ──────────────────────────────────────────────

@app.websocket("/ws/scrape")
async def websocket_endpoint(
    websocket: WebSocket,
    token: Optional[str] = Query(default=None),
):
    await websocket.accept()

    # Authenticate
    try:
        user = decode_token(token) if token else None
        if not user:
            await websocket.send_json({"type": "error", "message": "Not authenticated"})
            await websocket.close()
            return
        user_id  = int(user["sub"])
        username = user.get("username", "")
        team_id  = user.get("team_id", "cnk")
    except Exception:
        await websocket.send_json({"type": "error", "message": "Invalid token"})
        await websocket.close()
        return

    try:
        data        = await websocket.receive_json()
        site_key    = data.get("site")
        keywords    = data.get("keywords", [])
        credentials = data.get("credentials", {})
        loop        = asyncio.get_running_loop()

        # Live report card for this run — fed by the same log stream the
        # user sees, plus exact saved counts from result_cb below.
        stats          = RunStatsCollector(site=site_key)
        _last_progress = {"t": 0.0}

        def _send_progress(force: bool = False):
            now = time.monotonic()
            if not force and now - _last_progress["t"] < 2.0:
                return
            _last_progress["t"] = now
            snap = stats.snapshot()
            snap["total_keywords"] = len(keywords)
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "progress", "data": snap}), loop
            )

        def log_cb(msg):
            stats.feed(msg)
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "log", "message": msg}), loop
            )
            _send_progress()

        # Create session and track tenders per keyword
        session_id     = _db.create_session(user_id, site_key, team_id=team_id)
        keyword_counts: dict[str, int] = {}

        def result_cb(record):
            stats.record_saved()
            _send_progress(force=True)
            kw = record.get("keyword", "")
            keyword_counts[kw] = keyword_counts.get(kw, 0) + 1

            # Compute a safe relative path so the client can request a per-tender download.
            # Forward slashes only — works on both Windows and Linux path resolution.
            tender_dir_rel = ""
            tender_dir_abs = record.get("tender_dir", "")
            if tender_dir_abs:
                try:
                    tender_dir_rel = str(
                        Path(tender_dir_abs).relative_to(DOWNLOADS_DIR)
                    ).replace("\\", "/")
                except ValueError:
                    tender_dir_rel = ""

            _db.record_found_tender(
                session_id, kw,
                record.get("title", "Unknown"),
                record.get("url", ""),
                record.get("site", site_key),
                summary=record.get("fields"),
                tender_dir=tender_dir_rel,
                team_id=team_id,
            )
            _db.upsert_session_keyword(session_id, kw, keyword_counts[kw])

            payload = {
                "keyword":    kw,
                "title":      record.get("title", "Unknown"),
                "url":        record.get("url", ""),
                "site":       record.get("site", ""),
                "fields":     {k: str(v) for k, v in (record.get("fields") or {}).items()},
                "tender_dir": tender_dir_rel,
            }
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "result", "data": payload}), loop
            )

        def run_scrape():
            zip_path = None
            try:
                ts = now_ist_naive().strftime("%Y%m%d_%H%M%S")

                # Determine scraper type from team config
                try:
                    cfg_dir = APP_DIR / "configs" / "teams" / team_id
                    sites_cfg_file = cfg_dir / "sites_config.json" if (cfg_dir / "sites_config.json").exists() else APP_DIR / "sites_config.json"
                    with open(sites_cfg_file) as _f:
                        _sites_cfg = json.load(_f)
                    _scraper_type = _sites_cfg.get(site_key, {}).get("scraper_type", "standard")
                except Exception:
                    _scraper_type = "standard"

                if site_key == "ungm":
                    result = _run_ungm_scrape(keywords, credentials, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        zip_path = _make_run_zip([result], result, f"UNGM_{ts}")
                elif _scraper_type == "nasscom":
                    result = _run_nasscom_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "au":
                    result = _run_au_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "acbf":
                    result = _run_acbf_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "trademarkafrica":
                    result = _run_trademarkafrica_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "worldbank":
                    result = _run_worldbank_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "fhi360":
                    result = _run_fhi360_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "gatsbyafrica":
                    result = _run_gatsbyafrica_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "afrosai":
                    result = _run_afrosai_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "jsi":
                    result = _run_jsi_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "drc":
                    result = _run_drc_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "chai":
                    result = _run_chai_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "reliefweb":
                    result = _run_reliefweb_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                elif _scraper_type == "ngobox":
                    result = _run_ngobox_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")
                else:
                    result = _run_standard_scrape(site_key, keywords, log_cb, result_cb, team_id=team_id)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")

                _db.update_session_status(session_id, "complete")

                # Final report card — sent before "complete" so the client
                # renders the summary the moment the run ends.
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "summary", "data": stats.to_dict()}),
                    loop,
                )

                if result and zip_path and zip_path.exists():
                    _db.update_session_zip(session_id, zip_path.name)
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "complete", "zip": zip_path.name}), loop
                    )
                elif result:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "error",
                                            "message": "ZIP creation failed unexpectedly."}), loop
                    )
                else:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "complete", "zip": None}), loop
                    )

            except Exception as e:
                _db.update_session_status(session_id, "failed")
                import traceback
                err_repr = f"{type(e).__name__}: {e!r}"
                log_cb(f"❌ Fatal: {err_repr}")
                for line in traceback.format_exc().strip().splitlines():
                    if line.strip():
                        log_cb(f"   {line}")
                # Partial report card — show what was covered before the crash
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "summary", "data": stats.to_dict()}),
                    loop,
                )
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "error", "message": err_repr}), loop
                )

        _db.log_activity(user_id, username, "scrape_start",
                         details={"site": site_key, "keywords": keywords})

        async def _keepalive():
            # Slow sites (e.g. AfDB re-navigates the results list for every
            # row) can go 30-60s without emitting anything. Idle proxies and
            # tunnels then drop the socket, which the browser reports as
            # "Connection closed unexpectedly". Ping every 20s to keep it open.
            try:
                while True:
                    await asyncio.sleep(20)
                    await websocket.send_json({"type": "ping"})
            except Exception:
                pass

        keepalive_task = asyncio.create_task(_keepalive())
        try:
            await asyncio.to_thread(run_scrape)
        finally:
            keepalive_task.cancel()

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WS Error: {e}")
