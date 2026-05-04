import os
import re
import json
import asyncio
import platform
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from openpyxl import Workbook
from dotenv import load_dotenv

from agents.scraper_agent import ScraperAgent
from agents.summarizer_agent import SummarizerAgent
from paths import APP_DIR, OUTPUTS_DIR, DOWNLOADS_DIR, init as _init_paths

# Load .env from the app directory so double-click launch works regardless of cwd
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
async def download_file(file: str):
    if not os.path.exists(file):
        return {"error": "File not found"}
    ext = Path(file).suffix.lower()
    mime_map = {
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    }
    media_type = mime_map.get(ext, "application/octet-stream")
    return FileResponse(file, media_type=media_type, filename=os.path.basename(file))


# ── Standard scraper (ngobox / devnet) ──────────────────────────────────────

def _run_standard_scrape(site_key: str, keywords: list, log_cb) -> str | None:
    from agents.excel_writer import write_level1_report
    agent = ScraperAgent(str(APP_DIR / "sites_config.json"))
    summarizer = SummarizerAgent()

    excel_paths = []
    log_cb("🚀 Starting Agentic Scraper...")

    def make_callback(kw):
        """Return a per-keyword callback that fires immediately after each result is scraped."""
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
            excel_paths.append(excel_path)
            log_cb(f"✅ Saved: {excel_path}")
        return on_result_ready

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        # Pass callback into search() so it fires during the browser session,
        # immediately after each result is downloaded — not after all are done.
        agent.search(site_key, kw, log_callback=log_cb, on_result_ready=make_callback(kw))

    if not excel_paths:
        log_cb("⚠️ No results found.")
        return None

    log_cb(f"✅ Done. {len(excel_paths)} Level 1 Excel(s) saved.")
    return excel_paths[0]


# ── UNGM scraper ─────────────────────────────────────────────────────────────

def _run_ungm_scrape(keywords: list, credentials: dict, log_cb) -> str | None:
    from agents.ungm_scraper_agent import UNGMScraperAgent
    from agents.file_reader import read_file
    from agents.excel_writer import write_level1_report

    email = credentials.get("email", "").strip()
    password = credentials.get("password", "")
    show_browser = credentials.get("show_browser", False)

    # On Linux/Docker, DISPLAY must point to a real X server.
    # entrypoint.sh sets it to host.docker.internal:0.0 (VcXsrv) or falls back to :99 (Xvfb/noVNC).
    # On Windows/Mac, Playwright opens native windows without DISPLAY.
    if show_browser and platform.system() == "Linux" and not os.getenv("DISPLAY"):
        show_browser = False
        log_cb("ℹ️ No display server available — running headless.")

    if not email or not password:
        log_cb("❌ UNGM email and password are required.")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = str(DOWNLOADS_DIR / "ungm" / timestamp)

    summarizer = SummarizerAgent()
    excel_paths = []

    def on_tender_ready(res):
        """Fires immediately after each tender is downloaded — summarise and save Excel now."""
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

        # Save Excel inside the tender's own folder — alongside the downloaded PDFs
        tender_dir = Path(res.get("tender_dir", run_dir))
        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
        excel_path = str(tender_dir / f"Level1_{safe_title}.xlsx")
        write_level1_report([record], excel_path)
        excel_paths.append(excel_path)
        log_cb(f"✅ Saved: {excel_path}")

    log_cb("🚀 Starting UNGM Agentic Scraper...")
    if show_browser:
        log_cb("👁️ Live browser mode enabled — watch Chromium on your screen.")

    agent = UNGMScraperAgent()
    agent.scrape(email, password, keywords, run_dir,
                 headless=not show_browser, log_callback=log_cb,
                 on_tender_ready=on_tender_ready)

    if not excel_paths:
        log_cb("⚠️ No tenders found across all keywords.")
        return None

    log_cb(f"✅ Done. {len(excel_paths)} Level 1 Excel(s) saved in: {run_dir}")
    return excel_paths[0]


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
                if site_key == "ungm":
                    output_path = _run_ungm_scrape(keywords, credentials, log_cb)
                else:
                    output_path = _run_standard_scrape(site_key, keywords, log_cb)

                if output_path:
                    asyncio.run_coroutine_threadsafe(
                        websocket.send_json({"type": "complete", "file": output_path}), loop
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
