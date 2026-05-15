# Scraping Constraints & Rules

This file documents every constraint that applies to ALL sites (existing and future).
Read this before adding a new scraping target.

---

## 1. Date Filter — Last 24 Hours IST

**Rule:** A tender is downloaded only if its publication date falls within the last 24 hours from the time of the run, measured in IST (UTC +5:30).

**Rationale:** Covers tenders posted near midnight without missing anything published the previous evening on a late-running job. Deduplication handles repeat downloads across runs.

**Exception — `skip_date_filter`:** Sites that display only a *deadline* (not a publish date) set `"skip_date_filter": true` in `sites_config.json`. For these sites the date check is skipped entirely; deduplication alone prevents re-downloading tenders from previous runs. Currently applies to: **devnet**.

### How it works

| Layer | Where date comes from | Behaviour if not found |
|---|---|---|
| UNGM | `verified["Published on"]` field scraped from `span.label` elements; falls back to body text regex | Tender is **skipped** with a warning log |
| Standard sites (ngobox, …) | Optional `date_selector` CSS in `sites_config.json`; falls back to body text regex | Tender is **skipped** with a warning log |
| devnet | `skip_date_filter: true` — date check bypassed | N/A — dedup handles repeats |

### Date formats supported (auto-detected, no config needed)

```
07 May 2026        (DD Mon YYYY)
07-May-2026        (DD-Mon-YYYY)
2026-05-07         (ISO 8601)
07/05/2026         (DD/MM/YYYY)
07-05-2026         (DD-MM-YYYY)
07.05.2026         (DD.MM.YYYY)
May 07, 2026       (Month DD, YYYY)
2026-05-07T18:30:00+00:00  (ISO datetime with offset — converted to IST)
```

Dates in other timezones (UTC, EST, etc.) that include a time component are
**converted to IST** before the date comparison. Date-only strings are compared
as-is (no timezone shift applied — the day shown on the site is treated as the
publication day).

### Adding date support for a new site

Option A — CSS selector (preferred, most reliable):
```json
"my_site": {
  "date_selector": "span.posted-date"
}
```

Option B — Do nothing. The regex scanner will search the full page body text
for common date patterns near keywords like "published", "posted", "date posted".

---

## 2. Deduplication — No Repeat Downloads

**Rule:** A tender that has already been downloaded in any previous run is skipped,
even if it appears in results for a different keyword.

**Rationale:** Keywords like "e-learning", "elearning", and "e learning and capacity
building" can surface the same tender. Without deduplication the file would be
downloaded and summarised multiple times, wasting LLM quota and disk space.

### Storage

- **Database:** SQLite at `~/Documents/Tender Scrapping Documents/tender_tracker.db`
- **Table:** `downloaded_tenders`
- **Persists across runs** — restarting the app does NOT clear the dedup history.

### Deduplication logic (in order)

1. **URL match (primary):** Exact string equality. Catches the same tender found
   via multiple keywords — the most common case.
2. **Normalised title match (secondary):** Title is lowercased, punctuation
   stripped, whitespace collapsed. Catches tenders where the URL has query-string
   variants or redirects but the title is identical.

### When dedup fires

Both checks happen **before** the tender folder is created and **before** any
documents are downloaded. No disk writes occur for duplicates.

---

## 3. Standard Site Config (`sites_config.json`)

Every standard site must have these keys:

```json
"site_key": {
  "url":                      "https://example.com/search",
  "search_input_selector":    "input#search",
  "search_button_selector":   "button#submit",
  "results_link_selector":    "a.result-title",
  "tender_title_selector":    "h1.tender-title",
  "tender_description_selector": "div.description"
}
```

Optional keys:

| Key | Type | Purpose |
|---|---|---|
| `date_selector` | CSS string | Pinpoints the publication date element — more reliable than regex |
| `requires_auth` | bool | Set `true` if the site needs login; implement a dedicated agent class |
| `display_name` | string | Human-readable name shown in the UI |

---

## 4. UNGM-Specific Rules

- **Results cap:** `RESULTS_CAP = 10` tenders per keyword (set at top of `ungm_scraper_agent.py`).
- **Active-only filter:** Enabled automatically before each keyword search.
- **Date field used:** `Published on` from verified UNGM fields (ground truth).
- **Timezone:** UNGM dates are date-only (no time), so they are compared directly
  against today's IST date without timezone conversion.
- **Documents:** Quantum flow (SharePoint/Oracle) + direct `DownloadDocument` links.
  All ZIPs are recursively extracted.

---

## 5. Keyword Relevance Filter — Title Must Contain the Keyword

**Rule:** After the tender title is extracted, the search keyword must appear in that
title (case-insensitive substring match). If it does not, the tender is skipped
immediately — no content fetch, no date check, no dedup write, no LLM call.

**Rationale:** Search engines on tender sites often match against the description,
attachments, or metadata, not just the title. A keyword like `"training"` can surface
tenders that mention training in a footnote but are entirely unrelated to TMI's domain.
Pre-filtering by title prevents wasted Gemini quota and noisy Level 1 reports.

**Where the check lives:**

| Agent | File | Position in flow |
|---|---|---|
| Standard sites (devnet, ngobox, …) | `agents/scraper_agent.py` | After title extraction, before content fetch and date filter |
| UNGM | `agents/ungm_scraper_agent.py` | After title extraction and error-page check, before Quantum/download flow |

**Log message when skipped:**
```
🚫 Skipping '<title>' — keyword '<keyword>' not in title
```

**When adding a new site:**
No extra config needed. The check is built into `ScraperAgent.search()` for standard
sites. For a new auth-required site with its own agent, add the same guard immediately
after title extraction.

---

## 6. URL Stability — AJAX / Postback Sites

**Rule:** The URL stored for a tender must be a stable, shareable permalink that opens
the correct page in a cold browser session (no session state, no ViewState).

**Rationale:** ASP.NET WebForms sites (e.g. devnet) use postback navigation — after
clicking a result, `page.url` does not change. Storing `page.url` directly yields a
link to the search-results page, which is useless outside the live session.

**Multi-strategy extraction (in order, first hit wins):**

1. `page.url` already contains `job_id=` or equivalent — use it directly.
2. Scan the detail page for any `<a href="...">` whose href contains the identifier
   query param (e.g. `job_id=`).
3. Read the `<form action="...">` attribute — some postback pages set the job_id
   there after navigation.
4. Read the `<meta property="og:url">` tag.
5. If all strategies fail, log a warning and fall back to `page.url`; the link may
   not work but the tender content is still saved.

**When adding a new site:**
- Test whether `page.url` changes after clicking a result. If it does, no extra work.
- If the site uses AJAX/postback (URL stays the same), identify which query param
  uniquely identifies the tender (e.g. `job_id=`, `notice_id=`, `ref=`) and add it
  to the Strategy 2 selector in `scraper_agent.py`, or document it in a new agent.
- Record the stable URL pattern in the site's `sites_config.json` comment or here.

**DevNet URL pattern:** `https://devnetjobsindia.org/rfp_jobdetail.aspx?job_id=XXXXX`

---

## 7. Summarization Input Limits (Gemini)

**Rule:** Before sending content to the LLM, apply these caps:

| Cap | Value | Why |
|---|---|---|
| Per-file text | 25,000 chars | One large PDF can consume the entire combined budget |
| Combined prompt | 120,000 chars | Keeps token count within Gemini's safe TPM window during batch runs |

**Rationale:** A single UNGM tender can download 8+ PDFs totalling several MB of text.
Without per-file caps the combined prompt can exceed 500 K chars, reliably triggering
`RESOURCE_EXHAUSTED` (HTTP 429) on every request in a cron batch. After 3 retries the
summarizer returns `{}` and `write_level1_report` produces an empty template.

**Where the caps are applied:**
- Manual path: `api.py` `on_tender_ready()` — `_MAX_CHARS_PER_FILE = 25_000`,
  `max_chars=120_000` passed to `summarizer.summarize_level1()`.
- TAiQ Auto path: `cron_runner.py` `on_ungm_tender_ready()` — same constants, same
  call signature. **Both paths must stay in sync.**

**When adding a new site:**
Apply the same caps in any `on_result_ready` / `on_tender_ready` callback you write.
Never pass raw file text to the summarizer without truncating first.

---

## 8. Two Execution Paths — Manual vs TAiQ Auto

The system has two code paths that reach the same scraping agents. Constraints and
bugs in one path often do **not** automatically apply to the other.

| Aspect | Manual (WebSocket) | TAiQ Auto (Cron) |
|---|---|---|
| Entry point | `api.py` `/ws/scrape` | `cron_runner.py` `run_daily_job()` |
| Credentials | User-provided in session | `UNGM_EMAIL` / `UNGM_PASSWORD` env vars |
| Keywords | User selection | All keywords from `Keywords.json` |
| Dedup table | `downloaded_tenders` (global) | `cron_dedup` (global, persists across runs) |
| Log output | Live WebSocket stream → browser | `_write_log()` buffer → TAiQ dashboard + disk |
| Summarizer call | `summarize_level1(text, log_callback=log_cb, max_chars=120_000)` | Same — must stay in sync |

**Critical rule:** Any logic change to how tenders are filtered, summarized, or saved
in one path must be mirrored in the other. The two `on_tender_ready` callbacks
(`api.py` and `cron_runner.py`) are the most common place for divergence.

---

## 9. Adding a New Site (Checklist)

1. **Add entry to `sites_config.json`** with the required + any optional keys.
2. **Test CSS selectors** in browser DevTools before committing.
3. **Add `date_selector`** if the publication date has a reliable CSS selector;
   otherwise the regex fallback applies.
4. **Date available?** If the site only shows a deadline (no publish date), set
   `"skip_date_filter": true` — dedup will prevent re-processing.
5. **Auth required?** → Implement a new agent class in `agents/` modelled on
   `ungm_scraper_agent.py`, wire it into `api.py` like `_run_ungm_scrape()`,
   and pass `db=_db` into the agent's entry point.
6. **No auth?** → `ScraperAgent` handles it automatically; keyword filter, date
   filter, and dedup are already wired in.
7. **Check URL stability** (see § 6). Open the site, click a result, and check if
   the browser URL changes. If not, identify the job/notice ID query param and
   add it to the URL extraction logic.
8. **Keyword filter is automatic** for standard sites. For a custom agent, add a
   `keyword.lower() not in title.lower()` guard immediately after title extraction.
9. **Summarization caps**: if writing a custom `on_tender_ready`, apply the 25 K
   per-file and 120 K combined char caps before calling `summarize_level1()`.
10. **Mirror changes in both paths**: any new filtering logic must be in both the
    manual (`api.py`) callback and the TAiQ Auto (`cron_runner.py`) callback.
11. **Verify keyword filter works** — a keyword search for "training" should not
    return tenders whose titles are entirely unrelated.
12. **Verify dedup works** by running the scraper twice — second run should log
    `⏩ Duplicate skipped` for every tender from the first run.
13. **Verify date filter works** by checking that only today's tenders appear in
    the output folder.

---

## 10. File Layout

```
~/Documents/Tender Scrapping Documents/
  tender_tracker.db                          ← dedup database (persists forever)
  downloads/
    ungm/{YYYYMMDD_HHMMSS}/{keyword}/{title}/
    devnet/{keyword}/{title}/
    ngobox/{keyword}/{title}/
    {new_site}/{keyword}/{title}/
  outputs/
    {site}_{timestamp}.zip
    Level1_{title}.xlsx
```

---

## 11. Log Messages Reference

| Message | Meaning |
|---|---|
| `📅 Skipping '...' — published X (>24h ago)` | Date filter rejected the tender |
| `⚠️ No publication date found for '...' — skipping` | Date could not be extracted; tender skipped |
| `⏩ Duplicate skipped: ...` | Dedup check rejected the tender (seen in a previous run) |
| `🚫 Skipping '...' — keyword '...' not in title` | Keyword relevance filter rejected the tender |
| `⚠️ Could not find a stable job URL for '...' — link may not work` | AJAX/postback site; no job_id found via any URL strategy |
| `📂 Reading N file(s) for summarization...` | Attachment text extraction starting |
| `⚠️ Empty/unreadable: filename` | `read_file()` returned no text for this attachment |
| `⚠️ read_file error on filename: ...` | Exception during file text extraction |
| `📝 Combined content: X chars across Y section(s)` | Total input size being sent to Gemini |
| `❌ Nothing to summarize — page text empty and no readable files` | Summarizer received empty input; Level 1 will be a blank template |
| `⚠️ Summarizer returned no fields for: ...` | Gemini returned `{}` — likely rate-limited or safety-filtered |
| `✅ Logged in.` | UNGM session established |
| `⚠️ Session expired — redirected to login. Skipping.` | UNGM session dropped mid-run |
