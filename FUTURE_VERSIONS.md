# Future Versions — Expansion Roadmap

This file documents what is currently limited or disabled, and what becomes possible when
larger LLM context windows, paid API tiers, or local models are available.

---

## Deployment options

### Current: local launcher (`launch.bat` / `launch.py`)
- Double-click `launch.bat` → starts FastAPI server on `localhost:8000` → opens browser
- Anyone on the same WiFi / LAN can access it at `http://<your-machine-IP>:8000`
- Data saves to `~/Documents/Tender Scrapping Documents/` on the machine running the server
- **Best for**: 1–5 person internal team, one person runs the server, others browse to it

### Option A: Proper Windows installer (future)
Current state: users need Python installed and must run `pip install -r requirements.txt` + `playwright install chromium` manually.

To make a true one-click installer:
1. Use **Inno Setup** or **NSIS** to create a `.exe` installer that:
   - Installs a bundled Python environment (using an embedded Python zip)
   - Runs `pip install` and `playwright install chromium` silently during setup
   - Creates a Desktop shortcut pointing to `launch.bat`
2. **Why not PyInstaller directly**: Playwright bundles its own Chromium (~150 MB binary). PyInstaller can bundle it but the resulting `.exe` is 200–300 MB and fragile. An installer that ships dependencies separately is more reliable and easier to update.

### Option B: Cloud hosting (future)
Vercel and Netlify are serverless — Playwright cannot run there (no persistent process, no Chromium, 60s timeout).

**What works:**
- **Railway** (`railway.app`): push a `Dockerfile`, get a persistent server. Supports Playwright, WebSockets, env var secrets dashboard. ~$5/month.
- **Render** (`render.com`): same story, slightly simpler UI.
- **VPS** (DigitalOcean Droplet, Hetzner CX21): full control, SSH in, install deps, run with systemd or screen. Cheapest long-term (~$6/month).

**Required `Dockerfile` additions for cloud:**
```dockerfile
FROM python:3.11-slim
RUN apt-get update && apt-get install -y chromium chromium-driver
WORKDIR /app
COPY . .
RUN pip install -r requirements.txt
RUN playwright install chromium --with-deps
CMD ["uvicorn", "api:app", "--host", "0.0.0.0", "--port", "8000"]
```

**Data storage on cloud**: the local filesystem is ephemeral on most PaaS — files disappear on redeploy.
Options:
- Mount a persistent disk (Render/Railway both support this)
- Save outputs to S3 / Google Drive and return a signed download URL instead of a local file path
- For UNGM use, storing raw downloaded PDFs remotely adds significant complexity — the local/LAN approach is simpler

### Option C: Local network (current best for team use)
Run on one dedicated machine (even an old laptop):
1. Start `launch.bat`
2. Find your local IP: `ipconfig` → IPv4 Address (e.g. `192.168.1.42`)
3. Share `http://192.168.1.42:8000` with team members on the same network
4. All output saves to the machine running the server under `~/Documents/Tender Scrapping Documents/`

---

## Current hard limits (free Groq tier)

| Constraint | Current value | Why |
|------------|---------------|-----|
| Tokens per minute (TPM) | 12 000 | Groq free-tier cap |
| Input chars per tender | 28 000 (~9 800 tokens) | Leaves ~2 200 tokens for prompt + output, stays under TPM |
| Model | `llama-3.3-70b-versatile` | Best quality available on free tier |
| Retries on rate limit | 3 × 65 s backoff | TPM window is 60 s; 65 s gives a margin |
| Tenders per keyword (cap) | 10 | `RESULTS_CAP` in `ungm_scraper_agent.py` |
| Sites | UNGM, NGOBox, DevNet | Only these three wired up |

---

## What to unlock at each upgrade tier

### Tier 1 — Groq Dev / paid plan (~$5–20/month)

- **Raise `_MAX_CHARS_DEFAULT`** in `summarizer_agent.py` from 28 000 → 80 000+
  - A single large RFP PDF can be 60 000+ chars; currently truncated
  - Change: `_MAX_CHARS_DEFAULT = 80000` and remove or widen the retry backoff
- **Remove rate-limit sleeps** between summarisations — multiple tenders per minute becomes safe
- **Raise `RESULTS_CAP`** in `ungm_scraper_agent.py` from 10 → 25 or 50
- **Use a larger model**: `llama-3.1-405b` or `mixtral-8x7b` if needed for higher accuracy

### Tier 2 — Local LLM (Ollama / LM Studio, e.g. Mistral 24B or Llama 3.3 70B Q4)

- **No token limits at all** — process the entire 109 000 char combined document bundle
  - Change `max_chars` default to 200 000+
- **Parallel summarisation** — summarise multiple tenders at the same time using `asyncio.gather`
  - Currently blocked: Groq free tier would immediately 429; local has no such constraint
- **Zero API cost** — run unlimited tenders per day
- Integration: replace `Groq` client with an `openai`-compatible local endpoint:
  ```python
  # In summarizer_agent.py __init__:
  from openai import OpenAI
  self.client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
  # Model name becomes e.g. "llama3.3:70b" or "mistral:24b-instruct"
  ```

### Tier 3 — Claude API or GPT-4o (cloud, large context)

- 200 000 token context window (Claude) — entire tender + all attachments in one shot
- Higher accuracy on eligibility and quantum extraction (better instruction following)
- Integration: swap `Groq` for `anthropic.Anthropic()` or `openai.OpenAI()`

---

## Accuracy improvements (model-independent)

These can be done at any tier:

### Better PDF extraction
- **Current**: pdfplumber text + table rows concatenated
- **Upgrade**: use `pymupdf` (fitz) for layout-aware extraction — better column handling
- **Upgrade**: OCR fallback with `pytesseract` for scanned PDFs (common in UN documents)

### Structured field scraping (UNGM)
- **Current**: scrapes `span.label` elements for Reference, Deadline, Published on
- **Upgrade**: scrape the full details table (organisation, beneficiary countries, UNSPSC codes, notice type) and pass all as VERIFIED fields — more ground truth for the LLM

### Chunked summarisation (for very large documents)
- **Current**: truncates at `max_chars` — the tail of a long document is lost
- **Upgrade**: split into overlapping chunks → summarise each → merge summaries
  - Particularly useful for quantum and eligibility which appear deep in RFP appendices

### Multi-pass extraction
- **Current**: single LLM call for all 5 fields
- **Upgrade**: first pass extracts all text, second pass focuses only on financial tables for quantum
  - Reduces hallucination on the hardest fields

---

## New sites to add

Any site can be added by extending `sites_config.json` with the correct CSS selectors,
or by writing a dedicated agent (like `ungm_scraper_agent.py`) for sites that need login or AJAX handling.

| Site | Type | Notes |
|------|------|-------|
| **TED** (EU Tenders, ted.europa.eu) | Public | Large volume; may need pagination |
| **DgMarket** (dgmarket.com) | Public/Auth | World Bank procurement notices |
| **eTendering** (unops.org) | Auth required | Similar structure to UNGM |
| **GeBIZ** (Singapore) | Public | Government procurement portal |
| **GeM** (India) | Public | Government e-marketplace |

To add a standard site (no login, selector-based):
1. Add entry to `sites_config.json`
2. The existing `ScraperAgent` handles it automatically

To add a login-required site:
1. Create `agents/<sitename>_scraper_agent.py` following the pattern in `ungm_scraper_agent.py`
2. Add a credentials panel in `static/index.html`
3. Add a `_run_<sitename>_scrape()` function in `api.py`

---

## UI / UX improvements

- **Progress bar**: show tender N of M instead of only log lines
- **Keyword preset management**: save / load keyword sets from `Keywords.json` via the UI (currently read-only)
- **Scheduled runs**: cron-style — run every Monday morning and email the report
- **Deduplication**: track previously seen tender URLs in a local SQLite DB; skip re-summarising
- **Filter by deadline**: only include tenders with deadlines more than N days away
- **Export to Google Sheets**: using the Sheets API instead of / in addition to Excel

---

## Constants to change when upgrading

All tunable limits are in one place per file:

```python
# agents/summarizer_agent.py
_MAX_CHARS_DEFAULT = 28000      # raise to 80000+ on paid Groq, 200000+ on local LLM

# agents/ungm_scraper_agent.py
RESULTS_CAP = 10                # raise to 25–50 on paid tier / local LLM

# agents/summarizer_agent.py — retry block
wait_s = 65 * (attempt + 1)    # reduce or remove on paid tier with higher TPM
```
