# Tender Scrapper

Agentic tender intelligence tool. Scrapes procurement notices from configured websites, downloads attached documents, and uses an LLM to extract structured summaries into Excel or Word reports — all from a browser UI with a live log feed.

---

## Supported Sites

| Site | Output | Auth required |
|------|--------|---------------|
| **UNGM** (UN Global Marketplace) | Word `.docx` report | Yes — UNGM account, entered per session |
| **NGOBox** | Excel `.xlsx` report | No |
| **DevNet** | Excel `.xlsx` report | No |

---

## Architecture

```
Browser UI  (static/index.html + script.js + style.css)
      │  WebSocket  /ws/scrape
      ▼
FastAPI server  (api.py)
      │
      ├── ScraperAgent          agents/scraper_agent.py
      │     Playwright headless → ngobox / devnet
      │     keyword search → page text → .txt files → Excel report
      │
      ├── UNGMScraperAgent      agents/ungm_scraper_agent.py
      │     Playwright (headless or visible) → ungm.org
      │     Login → active-only filter → keyword search (cap: 10 tenders/keyword)
      │     → scrapes verified fields from span.label HTML elements (ground truth)
      │     → downloads all PDF / DOCX / XLSX attachments per tender
      │
      ├── FileReader             agents/file_reader.py
      │     PDF (pdfplumber — text + table rows), DOCX, XLSX, TXT
      │
      ├── SummarizerAgent        agents/summarizer_agent.py
      │     Groq API  llama-3.3-70b-versatile
      │     Input: labeled text bundle (28 000 char cap on free tier)
      │     Output: 5-field summary with confidence markers per field
      │       [VERIFIED]  /  [EXTRACTED]  /  [NOT_FOUND]  /  [MISMATCH: HIGH PRIORITY ERROR]
      │
      └── DocWriter              agents/doc_writer.py       ← UNGM
          ExcelWriter (inline)   api.py                     ← ngobox / devnet
```

### UNGM data flow

```
Login
  → per keyword:
      check Active-only filter → type keyword → click Search
      → collect up to 10 tender URLs from #tblNotices
      → for each tender:
            scrape span.label fields (Reference, Deadline on, Published on, …)
            scrape full page body text
            download all DownloadDocument attachments
              (each file opened in a separate browser page so main page stays alive)
            read each file with FileReader (text + tables)
      → build labeled text bundle sent to LLM:
            === VERIFIED FIELDS ===         ← LLM must treat as ground truth
            === UNGM NOTICE PAGE TEXT ===
            === ATTACHED DOCUMENT: name === ← one section per file
  → summarize (Groq, 28 000 char cap, auto-retry on rate limit)
  → write Word report  data/outputs/UNGM_Report_<timestamp>.docx
```

---

## Setup

### 1. Prerequisites

- Python 3.11+
- A [Groq](https://console.groq.com) account (free tier works for light use — see limits below)
- A UNGM vendor account if scraping UNGM

### 2. Install dependencies

```bash
pip install -r requirements.txt
playwright install chromium
```

With `uv`:

```bash
uv pip install -r requirements.txt
playwright install chromium
```

### 3. Environment variables

Create `.env` in the project root:

```
GEMINI_API_KEY= paste_your_key_here
```

### 4. Configuration files

**`sites_config.json`** — CSS selector config per site. UNGM is built in. Add ngobox / devnet entries with their selectors:

```json
{
  "ungm":   { "url": "https://www.ungm.org", "requires_auth": true },
  "ngobox": { "url": "...", "search_input_selector": "...", "search_button_selector": "...", "results_link_selector": "...", "tender_title_selector": "...", "tender_description_selector": "..." }
}
```

**`Keywords.json`** — keyword categories for the UI dropdown:

```json
{
  "E-Learning": ["Learning Management System (LMS)", "eLearning Platform", "..."],
  "Analytics":  ["Dashboard", "Data Visualization", "..."]
}
```

### 5. Run

**Option A — double-click (recommended for most users):**

Double-click `launch.bat`. It starts the server and opens your browser automatically.

**Option B — terminal:**

```bash
python launch.py
```

**Option C — developer mode (auto-reload on code changes):**

```bash
uvicorn api:app --reload
```

Open `http://localhost:8000`.

---

## Using the UI

1. **Select target website** from the dropdown.
2. **UNGM only:** enter your UNGM email and password. Credentials are used only for the current session — never stored. Enable "Show live browser on screen" to watch Chromium navigate in real time.
3. **Select keyword category** from the dropdown (pre-filled from `Keywords.json`), or type custom keywords comma-separated in the text area.
4. Click **Start Scraping**. The live terminal streams every action in real time.
5. When scraping finishes, click **Download Report** to save the output.

---

## Output files

All output is saved to `Documents\Tender Scrapping Documents\` in your Windows user profile — regardless of where the app is installed.

| Path | Contents |
|------|----------|
| `~/Documents/Tender Scrapping Documents/outputs/UNGM_Report_<timestamp>.docx` | Word report — one section per tender, 5 fields with colour-coded confidence badges |
| `~/Documents/Tender Scrapping Documents/outputs/Scrape_Results_<timestamp>.xlsx` | Excel report — ngobox / devnet results |
| `~/Documents/Tender Scrapping Documents/downloads/ungm/<timestamp>/<keyword>/<title>/` | Raw attachments downloaded per tender |
| `~/Documents/Tender Scrapping Documents/downloads/<site>/<keyword>_<n>_<title>.txt` | Raw page text for ngobox / devnet |

---

## UNGM Word report — field confidence badges

| Badge | Colour | Meaning |
|-------|--------|---------|
| `[✓ VERIFIED]` | Green | Value scraped directly from UNGM HTML — ground truth |
| `[~ EXTRACTED]` | Blue | Found in notice page text or attached documents by LLM |
| `[✗ NOT FOUND]` | Red italic | Genuinely absent from all content provided |
| `[⚠ MISMATCH: HIGH PRIORITY ERROR]` | Bold red | VERIFIED field contradicts the attached documents — both values quoted |

**Deadline** is always sourced from the `Deadline on` label on the UNGM page (VERIFIED), never inferred by the LLM. If a document states a different date, a MISMATCH flag is raised.

## Project structure

```
api.py                          FastAPI server + WebSocket endpoint
agents/
  scraper_agent.py              NGOBox / DevNet Playwright scraper
  ungm_scraper_agent.py         UNGM Playwright scraper
  file_reader.py                PDF / DOCX / XLSX / TXT text extractor
  summarizer_agent.py           Groq LLM wrapper + retry logic
  doc_writer.py                 Word report generator
static/
  index.html                    Browser UI
  script.js                     WebSocket client + UI logic
  style.css                     Dark glassmorphism theme
sites_config.json               Selector config per site
Keywords.json                   Keyword categories for UI dropdown
.env                            GROQ_API_KEY (not committed)
requirements.txt                Python dependencies
FUTURE_VERSIONS.md              Expansion roadmap and current limitations
```
