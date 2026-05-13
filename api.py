import os
import re
import json
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

app.include_router(auth_router)
app.include_router(admin_router)
app.include_router(dashboard_router)
app.include_router(superadmin_router)
app.include_router(taiq_router)

# ── Seed superadmin on first startup ──────────────────────────────────────

def _seed_superadmin():
    if _db.superadmin_exists():
        return
    username = os.getenv("SUPERADMIN_USERNAME", "superadmin")
    email    = os.getenv("SUPERADMIN_EMAIL",    "admin@taiq.local")
    password = os.getenv("SUPERADMIN_PASSWORD", "changeme123")
    _db.create_user(username, email, hash_password(password), "superadmin")
    print(f"[TAiQ] Superadmin seeded — username: '{username}'")
    if password == "changeme123":
        print("[TAiQ] WARNING: using default password. Set SUPERADMIN_PASSWORD in .env")

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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/config")
async def get_config():
    with open(APP_DIR / "sites_config.json", "r") as f:
        sites = json.load(f)
    try:
        with open(APP_DIR / "Keywords.json", "r") as f:
            keywords = json.load(f)
    except FileNotFoundError:
        keywords = {}
    return {"sites": list(sites.keys()), "keywords": keywords}


@app.get("/download")
async def download_file(name: str, token: Optional[str] = Query(default=None)):
    if not token:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)
    try:
        decode_token(token)
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
        decode_token(token)
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
        decode_token(token)
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
                         result_cb=None) -> list[Path] | None:
    from agents.excel_writer import write_level1_report
    agent = ScraperAgent(str(APP_DIR / "sites_config.json"))
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

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(site_key, kw, log_callback=log_cb,
                     on_result_ready=make_callback(kw), db=_db)

    if not tender_dirs:
        log_cb("⚠️ No results found.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── UNGM scraper ───────────────────────────────────────────────────────────

def _run_ungm_scrape(keywords: list, credentials: dict, log_cb,
                     result_cb=None) -> Path | None:
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

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
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
                 on_tender_ready=on_tender_ready, db=_db)

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

        def log_cb(msg):
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "log", "message": msg}), loop
            )

        # Create session and track tenders per keyword
        session_id     = _db.create_session(user_id, site_key)
        keyword_counts: dict[str, int] = {}

        def result_cb(record):
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
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")

                if site_key == "ungm":
                    result = _run_ungm_scrape(keywords, credentials, log_cb, result_cb)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        zip_path = _make_run_zip([result], result, f"UNGM_{ts}")
                else:
                    result = _run_standard_scrape(site_key, keywords, log_cb, result_cb)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")

                _db.update_session_status(session_id, "complete")

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
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "error", "message": err_repr}), loop
                )

        _db.log_activity(user_id, username, "scrape_start",
                         details={"site": site_key, "keywords": keywords})

        await asyncio.to_thread(run_scrape)

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WS Error: {e}")
