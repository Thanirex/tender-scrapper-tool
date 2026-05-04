import os
import re
import json
import asyncio
import platform
import zipfile
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse
from openpyxl import Workbook
from dotenv import load_dotenv

from agents.scraper_agent import ScraperAgent
from agents.summarizer_agent import SummarizerAgent
from paths import APP_DIR, OUTPUTS_DIR, DOWNLOADS_DIR, init as _init_paths

load_dotenv(APP_DIR / ".env")

_init_paths()

app = FastAPI()

app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
app.mount("/assets", StaticFiles(directory=str(APP_DIR / "Assets")), name="assets")


@app.get("/")
async def read_index():
    return FileResponse(str(APP_DIR / "static" / "index.html"))


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
async def download_file(name: str):
    file_path = (OUTPUTS_DIR / name).resolve()
    # Prevent path traversal — only serve files inside OUTPUTS_DIR
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
    media_type = mime_map.get(ext, "application/octet-stream")
    return FileResponse(str(file_path), media_type=media_type, filename=name)


def _make_run_zip(dirs: list[Path], base: Path, zip_stem: str) -> Path:
    """Zip all files under each dir (relative to base) into OUTPUTS_DIR/{zip_stem}.zip."""
    zip_path = OUTPUTS_DIR / f"{zip_stem}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for src in dirs:
            if not src.is_dir():
                continue
            for fpath in src.rglob("*"):
                if fpath.is_file():
                    zf.write(fpath, fpath.relative_to(base))
    return zip_path


# ── Standard scraper (ngobox / devnet) ──────────────────────────────────────

def _run_standard_scrape(site_key: str, keywords: list, log_cb) -> list[Path] | None:
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
            }
            tender_dir = Path(res["tender_dir"])
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
            excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")
            write_level1_report([record], excel_path)
            tender_dirs.append(tender_dir)
            log_cb(f"✅ Saved: {excel_path}")
        return on_result_ready

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        agent.search(site_key, kw, log_callback=log_cb, on_result_ready=make_callback(kw))

    if not tender_dirs:
        log_cb("⚠️ No results found.")
        return None

    log_cb(f"✅ Done. {len(tender_dirs)} tender(s) saved.")
    return tender_dirs


# ── UNGM scraper ─────────────────────────────────────────────────────────────

def _run_ungm_scrape(keywords: list, credentials: dict, log_cb) -> Path | None:
    from agents.ungm_scraper_agent import UNGMScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report

    email = credentials.get("email", "").strip()
    password = credentials.get("password", "")
    show_browser = credentials.get("show_browser", False)

    if show_browser and platform.system() == "Linux" and not os.getenv("DISPLAY"):
        show_browser = False
        log_cb("ℹ️ No display server available — running headless.")

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
            lines = ["=== VERIFIED FIELDS (scraped directly from UNGM — treat as ground truth) ==="]
            for k, v in verified.items():
                lines.append(f"{k}: {v}")
            text_parts.append("\n".join(lines))

        page_text = res.get("page_text", "").strip()
        if page_text:
            text_parts.append(f"=== UNGM NOTICE PAGE TEXT ===\n{page_text}")

        for fpath in res.get("files", []):
            file_text = read_file(fpath)
            if file_text and file_text.strip():
                fname = os.path.basename(fpath)
                text_parts.append(f"=== ATTACHED DOCUMENT: {fname} ===\n{file_text.strip()}")

        combined = "\n\n".join(text_parts)
        fields = summarizer.summarize_level1(combined, log_callback=log_cb)

        record = {
            "keyword": res["keyword"],
            "title": title,
            "url": res["url"],
            "site": "ungm",
            "fields": fields,
            "files": res.get("files", []),
        }

        tender_dir = Path(res.get("tender_dir", str(run_dir)))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")
        write_level1_report([record], excel_path)
        excel_paths.append(excel_path)
        log_cb(f"✅ Saved: {excel_path}")

    log_cb("🚀 Starting UNGM Agentic Scraper...")
    if show_browser:
        log_cb("👁️ Live browser mode enabled — watch Chromium on your screen.")

    agent = UNGMScraperAgent()
    agent.scrape(email, password, keywords, str(run_dir),
                 headless=not show_browser, log_callback=log_cb,
                 on_tender_ready=on_tender_ready)

    if not excel_paths:
        log_cb("⚠️ No tenders found across all keywords.")
        return None

    log_cb(f"✅ Done. {len(excel_paths)} Level 1 Excel(s) saved in: {run_dir}")
    return run_dir


# ── WebSocket endpoint ────────────────────────────────────────────────────────

@app.websocket("/ws/scrape")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    try:
        data = await websocket.receive_json()
        site_key = data.get("site")
        keywords = data.get("keywords", [])
        credentials = data.get("credentials", {})

        loop = asyncio.get_running_loop()

        def log_cb(msg):
            asyncio.run_coroutine_threadsafe(
                websocket.send_json({"type": "log", "message": msg}), loop
            )

        def run_scrape():
            try:
                ts = datetime.now().strftime("%Y%m%d_%H%M%S")

                if site_key == "ungm":
                    result = _run_ungm_scrape(keywords, credentials, log_cb)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        zip_path = _make_run_zip([result], result, f"UNGM_{ts}")
                else:
                    result = _run_standard_scrape(site_key, keywords, log_cb)
                    if result:
                        log_cb("📦 Packaging all results into ZIP...")
                        base = DOWNLOADS_DIR / site_key
                        zip_path = _make_run_zip(result, base, f"{site_key}_{ts}")

                if result and zip_path.exists():
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "complete", "zip": zip_path.name}), loop
                    )
                else:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "error", "message": "No output generated."}), loop
                    )
            except Exception as e:
                import traceback
                err_repr = f"{type(e).__name__}: {e!r}"
                log_cb(f"❌ Fatal: {err_repr}")
                for line in traceback.format_exc().strip().splitlines():
                    if line.strip():
                        log_cb(f"   {line}")
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "error", "message": err_repr}), loop
                )

        await asyncio.to_thread(run_scrape)

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WS Error: {e}")
