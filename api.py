import os
import json
import asyncio
from pathlib import Path
from datetime import datetime

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from typing import List

from openpyxl import Workbook
from openpyxl.styles import Font, Alignment, PatternFill
from dotenv import load_dotenv

from agents.scraper_agent import ScraperAgent
from agents.summarizer_agent import SummarizerAgent
from paths import APP_DIR, OUTPUTS_DIR, DOWNLOADS_DIR, init as _init_paths

# Load .env from the app directory so double-click launch works regardless of cwd
load_dotenv(APP_DIR / ".env")

_init_paths()

app = FastAPI()

app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")


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

def _format_excel(excel_path):
    from openpyxl import load_workbook
    wb = load_workbook(excel_path)
    ws = wb.active

    header_font = Font(bold=True, color="FFFFFF")
    header_fill = PatternFill("solid", fgColor="4F81BD")
    align_center = Alignment(horizontal="center", vertical="center")
    align_wrap = Alignment(wrap_text=True, vertical="top")

    for cell in ws[1]:
        cell.font = header_font
        cell.fill = header_fill
        cell.alignment = align_center

    ws.column_dimensions["A"].width = 6
    ws.column_dimensions["B"].width = 15
    ws.column_dimensions["C"].width = 20
    ws.column_dimensions["D"].width = 40
    ws.column_dimensions["E"].width = 80

    for row in range(2, ws.max_row + 1):
        ws.cell(row=row, column=5).alignment = align_wrap

    wb.save(excel_path)


def _run_standard_scrape(site_key: str, keywords: list, log_cb) -> str | None:
    agent = ScraperAgent(str(APP_DIR / "sites_config.json"))
    summarizer = SummarizerAgent()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_path = str(OUTPUTS_DIR / f"Scrape_Results_{timestamp}.xlsx")

    columns = ["S.no", "Website", "Keyword", "Document Name", "Summary"]
    records = []
    s_no = 1

    log_cb("🚀 Starting Agentic Scraper...")

    for kw in keywords:
        log_cb(f"▶️ Processing keyword: {kw}")
        results = agent.search(site_key, kw, log_callback=log_cb)
        if not results:
            log_cb(f"   ↳ No results for '{kw}'")
            continue
        for res in results:
            summary = summarizer.summarize(res.get("content", ""), log_callback=log_cb)
            records.append([s_no, site_key, kw, res.get("title", "Unknown"), summary])
            s_no += 1

    wb = Workbook()
    ws = wb.active
    ws.append(columns)
    for row in records:
        ws.append(row)
    OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    _format_excel(output_path)

    log_cb(f"✅ Done. Excel saved: {output_path}")
    return output_path


# ── UNGM scraper ─────────────────────────────────────────────────────────────

def _run_ungm_scrape(keywords: list, credentials: dict, log_cb) -> str | None:
    from agents.ungm_scraper_agent import UNGMScraperAgent
    from agents.file_reader import read_file
    from agents.doc_writer import write_report

    email = credentials.get("email", "").strip()
    password = credentials.get("password", "")
    show_browser = credentials.get("show_browser", False)

    if not email or not password:
        log_cb("❌ UNGM email and password are required.")
        return None

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = str(DOWNLOADS_DIR / "ungm" / timestamp)
    output_path = str(OUTPUTS_DIR / f"UNGM_Report_{timestamp}.docx")

    log_cb("🚀 Starting UNGM Agentic Scraper...")
    if show_browser:
        log_cb("👁️ Live browser mode enabled — watch Chromium on your screen.")
    agent = UNGMScraperAgent()
    raw_results = agent.scrape(email, password, keywords, run_dir, headless=not show_browser, log_callback=log_cb)

    if not raw_results:
        log_cb("⚠️ No tenders found across all keywords.")
        return None

    log_cb(f"📋 Building summaries for {len(raw_results)} tenders...")
    summarizer = SummarizerAgent()
    records = []

    for res in raw_results:
        text_parts = []

        # Verified fields scraped directly from UNGM structured HTML — ground truth
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
        log_cb(f"🤖 Summarizing: {res['title'][:60]}... ({len(combined):,} chars)")
        summary = summarizer.summarize(combined, log_callback=log_cb)

        records.append({
            "keyword": res["keyword"],
            "title": res["title"],
            "url": res["url"],
            "summary": summary,
            "files": res.get("files", []),
        })

    write_report(records, output_path)
    log_cb(f"✅ Word report saved: {output_path}")
    return output_path


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
                log_cb(f"❌ Fatal: {str(e)}")
                asyncio.run_coroutine_threadsafe(
                    websocket.send_json({"type": "error", "message": str(e)}), loop
                )

        await asyncio.to_thread(run_scrape)

    except WebSocketDisconnect:
        print("Client disconnected")
    except Exception as e:
        print(f"WS Error: {e}")
