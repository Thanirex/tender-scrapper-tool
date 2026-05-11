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

## 5. Adding a New Site (Checklist)

1. **Add entry to `sites_config.json`** with the required + any optional keys.
2. **Test CSS selectors** in browser DevTools before committing.
3. **Add `date_selector`** if the publication date has a reliable CSS selector;
   otherwise the regex fallback applies.
4. **Auth required?** → Implement a new agent class in `agents/` modelled on
   `ungm_scraper_agent.py`, wire it into `api.py` like `_run_ungm_scrape()`,
   and pass `db=_db` into the agent's entry point.
5. **No auth?** → `ScraperAgent` handles it automatically; date filter and dedup
   are already wired in.
6. **Verify dedup works** by running the scraper twice — second run should log
   `⏩ Duplicate skipped` for every tender from the first run.
7. **Verify date filter works** by checking that only today's tenders appear in
   the output folder.

---

## 6. File Layout

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

## 7. Log Messages Reference

| Message | Meaning |
|---|---|
| `📅 Skipping '...' — published X (>24h ago)` | Date filter rejected the tender |
| `⚠️ No publication date found for '...' — skipping` | Date could not be extracted; tender skipped |
| `⏩ Duplicate skipped: ...` | Dedup check rejected the tender |
| `✅ Logged in.` | UNGM session established |
| `⚠️ Session expired — redirected to login. Skipping.` | UNGM session dropped mid-run |
