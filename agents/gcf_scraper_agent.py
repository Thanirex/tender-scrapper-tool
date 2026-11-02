"""Green Climate Fund (GCF) procurement scraper.

Portal: https://iaayou.fa.ocs.oraclecloud.com/fscmUI/faces/NegotiationAbstracts?prcBuId=300000003621906
(linked from https://www.greenclimate.fund/about/procurement; no login needed)

The portal is an Oracle Fusion (ADF) application, so Playwright is required: the
Details popup and its downloads are server-side postbacks tied to the browser
session, and attachments have no plain links. Verified structure (2026-11):

    listing:  one <tr> per notice, in this column order:
              Negotiation | Title | Type | Status | Posting | Open | Close | Details
              The Details cell holds a[id$=':commandLink1'].
    popup:    "Abstract: RFP RFx202600052" with label/value rows. The Synopsis is
              a read-only <textarea>, so its text is only visible via input_value().
              Attachments are list items whose only control is a download icon.

Design decisions that can look like omissions:

  * The portal's search box is not used. It matches titles by substring and costs
    about 4.5 s per search. Keywords are matched here with keyword_matches() on
    the title, which gives the same title-only behaviour without the substring
    false hits ("ILT" in "Desiltation"), for one page load per run.
  * Only Status=Active notices are collected. Every amendment is its own row
    (RFx202600037, RFx202600037,1, ...) and earlier rounds become "Amended", so
    filtering to Active leaves one row per tender that can still be bid on.
  * No publication-age window (matching ISA and Mercy Corps). GCF posts about three
    notices a month, so a 24 h window would miss most of them. The Active filter,
    the deadline gate and the dedup tables do that job instead.
  * Dedup URL uses the base notice number, so an amendment of a tender already
    collected counts as a duplicate even when its title was reworded. There is no
    per-tender page: this URL opens the full list, where the number can be searched.
  * Portal times are in Tokyo time (UTC+9, the same as the KST used in GCF's
    annexes) with two-digit years, which date_utils cannot parse. It fails open on
    them, so dates are converted to an IST YYYY-MM-DD before the deadline gate.
  * The browser identifies itself as Playwright's default. If the portal starts
    refusing it, report that rather than disguising the browser.
"""
import re
import sys
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches, find_negative_keyword
from date_utils import is_deadline_active, IST

# ── Selectors ────────────────────────────────────────────────────────────────
_SEARCH_INPUT  = "input[id$='sf1:it1::content']"     # only used to detect page load
_SHOW_FILTERS  = "button[id$='sf1:b2']"
_STATUS_SELECT = "select[id$='qryId1:value00::content']"
_FILTER_SEARCH = "button[id$='qryId1::search']"
_DETAILS_LINK  = "a[id$=':commandLink1']"
_SYNOPSIS      = "textarea[id$=':it2::content']"
_ATT_LIST      = "div[id$='dciAvsd:lvAvsd']"
_ATT_ITEM      = "div[id*='lvAvsd:'][id$=':lviAvsd']"
_ATT_NAME      = "span[id$='otAvsddDiFile']"
_ATT_LINK      = "a[id$='glAvsddDwn']"
_NEGOTIATION_PDF = "a[id$='r4:0:cl1']"
_POPUP_OK      = "OK"
_NO_DATA_TEXT  = "No data to display"

_READ_ROWS_JS = """links => links.map(a => {
    const tr = a.closest('tr');
    return tr ? [...tr.cells].map(td => td.innerText.trim()) : [];
})"""

# The results grid fetches about 21 rows at a time as it scrolls.
_SCROLL_GRID_JS = """() => document.querySelectorAll('div').forEach(d => {
    if (d.scrollHeight > d.clientHeight + 20
            && /auto|scroll/.test(getComputedStyle(d).overflowY)) {
        d.scrollTop = d.scrollHeight;
    }
})"""

# ── Timing ───────────────────────────────────────────────────────────────────
_NAV_TIMEOUT_MS      = 90_000
_ACTION_TIMEOUT_MS   = 30_000
_DOWNLOAD_TIMEOUT_MS = 60_000
_FILTER_TIMEOUT_S    = 40
_DOWNLOAD_ATTEMPTS   = 2
_LISTING_ATTEMPTS    = 2

# ── Dates ────────────────────────────────────────────────────────────────────
_PORTAL_TZ = timezone(timedelta(hours=9))
_PORTAL_DATE_FORMATS = ("%m/%d/%y %I:%M %p", "%m/%d/%y")

# ── Summarizer limits (SCRAPING_CONSTRAINTS section 7) ───────────────────────
MAX_CHARS_PER_FILE = 25_000
MAX_COMBINED_CHARS = 120_000

# ── Documents ────────────────────────────────────────────────────────────────
# Tender folders are zipped and served to users, so executables never go in one.
_BLOCKED_EXTS = {
    ".exe", ".scr", ".bat", ".cmd", ".com", ".pif", ".msi", ".msp",
    ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1",
    ".jar", ".dll", ".cpl", ".hta", ".reg", ".lnk", ".app",
}
_MAX_ZIP_BYTES = 200 * 1024 * 1024

# The portal lists the TOR last and contracts and forms before it. Text is cut at
# MAX_COMBINED_CHARS, so the documents that describe the work must go first.
_RANK_CORE, _RANK_SUPPORT, _RANK_OTHER, _RANK_BOILERPLATE = 0, 1, 2, 3
_CORE_DOC_RE = re.compile(
    r"(?<![a-z])(tor|sow)(?![a-z])|terms? of reference|scope of work|statement of work",
    re.IGNORECASE,
)
_SUPPORT_DOC_RE = re.compile(
    r"instruction|addendum|addenda|amendment|timeline|evaluation|how to submit",
    re.IGNORECASE,
)
_BOILERPLATE_DOC_RE = re.compile(
    r"contract|(?<![a-z])forms?(?![a-z])|template", re.IGNORECASE
)


def _doc_rank(name: str) -> int:
    if _CORE_DOC_RE.search(name):
        return _RANK_CORE
    if _SUPPORT_DOC_RE.search(name):
        return _RANK_SUPPORT
    if _BOILERPLATE_DOC_RE.search(name):
        return _RANK_BOILERPLATE
    return _RANK_OTHER


def _parse_portal_date(raw: str) -> "datetime | None":
    """Parse '11/13/26 5:59 PM' or '10/27/26' as a time in the portal's timezone."""
    text = (raw or "").strip()
    for fmt in _PORTAL_DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=_PORTAL_TZ)
        except ValueError:
            continue
    return None


def _ist_date_str(raw: str) -> str:
    """IST 'YYYY-MM-DD' for the deadline gate, or '' when the date can't be read."""
    dt = _parse_portal_date(raw)
    return dt.astimezone(IST).strftime("%Y-%m-%d") if dt else ""


def _display_date(raw: str) -> str:
    """'13 Nov 2026'. Two-digit US dates are too easy for a reader or the LLM to misread."""
    dt = _parse_portal_date(raw)
    return dt.strftime("%d %b %Y") if dt else (raw or "").strip()


def _display_datetime(raw: str) -> str:
    dt = _parse_portal_date(raw)
    if not dt:
        return (raw or "").strip()
    if ":" not in raw:
        return dt.strftime("%d %b %Y")
    return f"{dt.strftime('%d %b %Y, %I:%M %p').replace(', 0', ', ')} (UTC+9, Korea/Tokyo time)"


def _base_id(negotiation: str) -> str:
    """'RFx202600037,2' -> 'RFx202600037'."""
    return (negotiation or "").split(",")[0].strip()


def build_combined_text(res: dict, read_file, log) -> str:
    """Summarizer input for one GCF result: page text, then documents in rank order.

    Shared by the manual (api.py) and TAiQ (cron_runner.py) callbacks so the two
    paths can't drift apart (SCRAPING_CONSTRAINTS section 8).
    """
    parts = []
    page_text = (res.get("page_text") or "").strip()
    if page_text:
        parts.append(f"=== PAGE CONTENT ===\n{page_text}")

    for fpath in res.get("files", []):
        path = Path(fpath)
        if path.name == "page_content.txt":   # same text as page_text above
            continue
        try:
            text = read_file(str(path))
        except Exception as err:
            log(f"⚠️ read_file error: {err}")
            continue
        if text and text.strip():
            parts.append(
                f"=== DOCUMENT: {path.name} ===\n{text[:MAX_CHARS_PER_FILE].strip()}"
            )
    return "\n\n".join(parts)


class GreenClimateFundScraperAgent:
    LIST_URL = (
        "https://iaayou.fa.ocs.oraclecloud.com/fscmUI/faces/"
        "NegotiationAbstracts?prcBuId=300000003621906"
    )

    def __init__(self):
        # One instance is reused across every keyword of a run, so the portal is
        # loaded once per run, not once per keyword. A failed load is remembered
        # too: retrying it for each of 80+ keywords would hammer the site and cost
        # minutes of timeouts per keyword.
        self._cached_rows = None
        self._listing_failed = False

    # ── browser helpers ──────────────────────────────────────────────────────
    @staticmethod
    def _settle(page, idle_ms: int = 15_000, pause_ms: int = 800):
        try:
            page.wait_for_load_state("networkidle", timeout=idle_ms)
        except PlaywrightTimeout:
            pass   # Oracle's telemetry can keep the network busy; the pause still applies
        page.wait_for_timeout(pause_ms)

    def _open_portal(self, browser):
        ctx = browser.new_context(
            viewport={"width": 1500, "height": 1100}, accept_downloads=True
        )
        page = ctx.new_page()
        page.set_default_timeout(_ACTION_TIMEOUT_MS)
        response = page.goto(
            self.LIST_URL, wait_until="domcontentloaded", timeout=_NAV_TIMEOUT_MS
        )
        if response is not None and response.status >= 400:
            raise RuntimeError(f"portal answered HTTP {response.status}")
        page.wait_for_selector(_SEARCH_INPUT, state="visible", timeout=_NAV_TIMEOUT_MS)
        self._settle(page)
        return page

    @staticmethod
    def _read_rows(page) -> list:
        rows = []
        for cells in page.eval_on_selector_all(_DETAILS_LINK, _READ_ROWS_JS):
            if len(cells) < 7 or not cells[0] or not cells[1]:
                continue
            rows.append({
                "id":     cells[0],
                "title":  cells[1],
                "type":   cells[2],
                "status": cells[3],
                "posted": cells[4],
                "opens":  cells[5],
                "closes": cells[6],
            })
        return rows

    def _show_active_only(self, page) -> list:
        """Apply the portal's Status=Active filter and return the Active rows.

        Raises rather than returning [] when the filter can't be confirmed, so a
        changed page shows up as an error and not as "no tenders today".
        """
        if not page.locator(_STATUS_SELECT).is_visible():
            page.click(_SHOW_FILTERS)
            page.wait_for_selector(_STATUS_SELECT, state="visible")
        page.select_option(_STATUS_SELECT, label="Active")
        page.wait_for_timeout(500)
        page.click(_FILTER_SEARCH)

        give_up_at = time.monotonic() + _FILTER_TIMEOUT_S
        while time.monotonic() < give_up_at:
            rows = self._read_rows(page)
            if rows and all(r["status"] == "Active" for r in rows):
                return self._load_remaining_rows(page, rows)
            if not rows and _NO_DATA_TEXT in page.inner_text("body"):
                return []
            page.wait_for_timeout(500)
        raise RuntimeError("the Status=Active filter did not apply")

    def _load_remaining_rows(self, page, rows: list) -> list:
        """Scroll the grid until no new rows arrive. Rarely needed for Active notices."""
        seen = {r["id"]: r for r in rows}
        if len(seen) < 15:
            return rows
        stable = 0
        while stable < 2:
            before = len(seen)
            page.evaluate(_SCROLL_GRID_JS)
            page.wait_for_timeout(1_200)
            for row in self._read_rows(page):
                seen.setdefault(row["id"], row)
            stable = stable + 1 if len(seen) == before else 0
        return list(seen.values())

    def _fetch_rows(self, log) -> list:
        last_error = None
        for attempt in range(1, _LISTING_ATTEMPTS + 1):
            try:
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    try:
                        page = self._open_portal(browser)
                        return self._show_active_only(page)
                    finally:
                        browser.close()
            except Exception as err:
                last_error = err
                if attempt < _LISTING_ATTEMPTS:
                    log(f"   ↳ GCF portal didn't load ({type(err).__name__}), retrying...")
        raise last_error

    # ── main entry point ─────────────────────────────────────────────────────
    def search(self, keyword, output_dir=None, log_callback=None,
               on_result_ready=None, db=None, team_id="cnk"):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                try:
                    print(msg)
                except UnicodeEncodeError:
                    print(msg.encode("ascii", "replace").decode("ascii"))

        results = []
        log(f"🔍 [GCF] Scanning for '{keyword}'...")

        if self._listing_failed:
            log("   ↳ Skipped: the GCF portal could not be loaded earlier in this run")
            return results

        if self._cached_rows is None:
            try:
                self._cached_rows = self._fetch_rows(log)
                log(f"   ↳ {len(self._cached_rows)} active tender(s) listed on GCF")
            except Exception as err:
                self._listing_failed = True
                log(f"❌ GCF listing fetch error: {type(err).__name__}: {err}")
                return results
        else:
            log(f"   ↳ Using cached listing ({len(self._cached_rows)} active tender(s))")

        rows = self._cached_rows
        base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "gcf"

        n_miss = n_neg = n_closed = n_dup = n_opened = n_err = 0

        for row in rows:
            try:
                title = row["title"]
                url   = f"{self.LIST_URL}&ref={_base_id(row['id'])}"

                # 1. Positive keyword: title only, whole word (keyword_utils)
                if not keyword_matches(keyword, title):
                    n_miss += 1
                    continue

                # 2. Negative keywords, stage 1: on what the listing shows
                neg = find_negative_keyword(title, team_id=team_id)
                if neg:
                    n_neg += 1
                    log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}' in title")
                    continue

                # 3. Deadline gate
                close_date = _ist_date_str(row["closes"])
                if close_date and not is_deadline_active(close_date):
                    n_closed += 1
                    log(f"   ⏭ Skipping '{title[:60]}' — bidding closed on "
                        f"{_display_date(row['closes'])}")
                    continue

                # 4. Dedup
                if db and db.is_duplicate(title, url, team_id=team_id):
                    n_dup += 1
                    log(f"   ⏩ Duplicate: '{title[:60]}' ({row['id']}) — "
                        f"already collected in an earlier run")
                    continue

                n_opened += 1
                log(f"   📄 Opening: {row['id']} {title[:70]}")

                # The browser is closed before on_result_ready runs, so it isn't
                # held open during the slow summarizer call.
                rec = self._extract_detail(row, url, keyword, base, log, db=db, team_id=team_id)
                if rec:
                    results.append(rec)
                    if on_result_ready:
                        on_result_ready(rec)

            except Exception as row_err:
                n_err += 1
                log(f"   ⚠️ Row error: {row_err}")
                continue

        log(
            f"   📊 '{keyword}' summary on GCF: {len(rows)} tender(s) listed → "
            f"{n_miss} without the keyword, "
            f"{n_neg} blocked by negative keywords, "
            f"{n_closed} archived/expired, "
            f"{n_dup} already collected, "
            f"{n_opened} opened for full check, {n_err} errored, {len(results)} saved"
        )
        return results

    # ── Details popup ────────────────────────────────────────────────────────
    def _open_popup(self, page, row: dict):
        """Open the Details popup of one notice, confirming it's the right one."""
        ids = [r[0] if r else "" for r in page.eval_on_selector_all(_DETAILS_LINK, _READ_ROWS_JS)]
        if row["id"] not in ids:
            raise RuntimeError(f"{row['id']} is no longer listed as Active")
        page.locator(_DETAILS_LINK).nth(ids.index(row["id"])).click()
        heading = re.compile(rf"Abstract:.*{re.escape(_base_id(row['id']))}")
        page.get_by_text(heading).first.wait_for(state="visible", timeout=_ACTION_TIMEOUT_MS)
        self._settle(page)

    @staticmethod
    def _read_popup_fields(page) -> dict:
        """Label/value rows of the open popup, e.g. {'Buyer': ..., 'Close Date': ...}."""
        wanted = {
            "Title", "Negotiation Type", "Status", "Buyer", "Email",
            "Open Date", "Close Date", "Posting Date",
        }
        body = page.inner_text("body")
        fields = {}
        for line in body[body.rfind("Abstract:"):].splitlines():
            label, _, value = line.partition("\t")
            label, value = label.strip(), value.strip()
            if label in wanted and value and label not in fields:
                fields[label] = value
        return fields

    def _extract_detail(self, row, url, keyword, base_dir, log, db=None, team_id="cnk"):
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = self._open_portal(browser)
                self._show_active_only(page)
                self._open_popup(page, row)

                fields = self._read_popup_fields(page)
                title  = fields.get("Title") or row["title"]
                synopsis = (
                    page.input_value(_SYNOPSIS).strip()
                    if page.locator(_SYNOPSIS).count() else ""
                )

                # Negative keywords, stage 2: now that the synopsis is available
                neg = find_negative_keyword(title, synopsis, team_id=team_id)
                if neg:
                    log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on page")
                    if db:
                        db.mark_downloaded(title, url, "gcf", keyword, "", team_id=team_id)
                    return None

                safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
                safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:35].strip("_. ")
                tender_dir = base_dir / safe_kw / safe_title
                tender_dir.mkdir(parents=True, exist_ok=True)

                ranked_files, doc_lines = self._download_documents(page, tender_dir, log)
            finally:
                browser.close()

        posted = fields.get("Posting Date") or row["posted"]
        closes = fields.get("Close Date") or row["closes"]
        opens  = fields.get("Open Date") or row["opens"]

        detail_lines = [
            f"Bid Title: {title}",
            "Inviting Authority: Green Climate Fund (GCF)",
            f"Negotiation Number: {row['id']}",
            f"Negotiation Type: {fields.get('Negotiation Type') or row['type']}",
            f"Status: {fields.get('Status') or row['status']}",
        ]
        if fields.get("Buyer"):
            detail_lines.append(f"Buyer: {fields['Buyer']}")
        if fields.get("Email"):
            detail_lines.append(f"Email: {fields['Email']}")
        detail_lines.extend([
            f"Publication Date: {_display_date(posted)}",
            f"Open Date: {_display_datetime(opens)}",
            f"Submission Deadline: {_display_datetime(closes)}",
        ])
        if synopsis:
            detail_lines.extend(["", "Synopsis:", synopsis])
        if doc_lines:
            detail_lines.extend(["", "=== DOCUMENTS ===", *doc_lines])
        combined_text = "\n".join(detail_lines).strip()

        txt_path = tender_dir / "page_content.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"Source: {url}\n\n{combined_text}")

        files = [path for _, path in sorted(ranked_files, key=lambda item: item[0])]
        if not files:
            files.append(str(txt_path))
            log("      📝 No documents — saved tender text only")
        else:
            log("      📝 Tender text also saved")

        published = _display_date(posted)
        deadline  = _display_date(closes)

        if db:
            db.mark_downloaded(title, url, "gcf", keyword, published or deadline, team_id=team_id)

        return {
            "keyword":        keyword,
            "title":          title,
            "url":            url,
            "page_text":      combined_text,
            "files":          files,
            "tender_dir":     str(tender_dir),
            "site":           "gcf",
            "published_date": published,
            "deadline":       deadline,
        }

    # ── Downloads ────────────────────────────────────────────────────────────
    def _download_documents(self, page, tender_dir: Path, log) -> tuple:
        """Download every attachment plus the Negotiation PDF from the open popup.

        Returns ([(rank, path), ...], doc_lines). Raises if documents were offered
        but none arrived: the tender then stays uncollected, so a later run retries
        it instead of it being permanently saved with no documents.
        """
        ranked, doc_lines = [], []
        offered = failed = 0

        items = page.locator(_ATT_ITEM)
        total = items.count()
        declared = page.locator(_ATT_LIST).first.get_attribute("_afrtotalrows") if total else None
        log(f"      📎 {total} attachment(s) listed")
        if declared and declared.isdigit() and int(declared) > total:
            log(f"      ⚠️ Portal declares {declared} attachments but only {total} rendered")

        for i in range(total):
            item = items.nth(i)
            name_el = item.locator(_ATT_NAME)
            name = name_el.first.inner_text().strip() if name_el.count() else f"attachment_{i + 1}"
            size = re.search(r"\(([\d.,]+\s*[KMG]?B)\)", item.inner_text())
            size_note = f" ({size.group(1)})" if size else ""

            link = item.locator(_ATT_LINK)
            if not link.count():
                # Attachment rows can also be web links, which have no download icon.
                log(f"      ⚠️ No download offered for '{name}'")
                doc_lines.append(f"{name}{size_note} — not a downloadable file")
                continue

            offered += 1
            saved = self._download_one(page, link.first, tender_dir, name, log)
            if saved is None:
                failed += 1
                doc_lines.append(f"{name}{size_note} — DOWNLOAD FAILED, check the portal")
                continue
            doc_lines.append(f"{name}{size_note}")
            rank = _doc_rank(name)
            ranked.extend((rank, path) for path in saved)

        pdf_link = page.locator(_NEGOTIATION_PDF)
        if pdf_link.count():
            # Holds the notice's Instructions to Bidders.
            offered += 1
            saved = self._download_one(page, pdf_link.first, tender_dir, "Negotiation PDF", log)
            if saved is None:
                failed += 1
                doc_lines.append("Negotiation PDF — DOWNLOAD FAILED, check the portal")
            else:
                doc_lines.append("Negotiation PDF")
                ranked.extend((_RANK_SUPPORT, path) for path in saved)

        if offered and failed == offered:
            raise RuntimeError(f"all {offered} document downloads failed")
        if failed:
            log(f"      ⚠️ {failed} of {offered} documents could not be downloaded")
        return ranked, doc_lines

    def _download_one(self, page, link, tender_dir: Path, name: str, log) -> "list | None":
        """Returns the saved paths ([] if deliberately refused), or None on failure."""
        for attempt in range(1, _DOWNLOAD_ATTEMPTS + 1):
            try:
                link.scroll_into_view_if_needed()
                with page.expect_download(timeout=_DOWNLOAD_TIMEOUT_MS) as info:
                    link.click()
                return self._save_download(info.value, tender_dir, log)
            except Exception as err:
                if attempt == _DOWNLOAD_ATTEMPTS:
                    log(f"      ⚠️ Download failed ('{name[:60]}'): {type(err).__name__}")
                page.wait_for_timeout(1_500)
        return None

    def _save_download(self, download, tender_dir: Path, log) -> list:
        fname = Path(download.suggested_filename or "document").name
        ext   = Path(fname).suffix[:10]

        if ext.lower() in _BLOCKED_EXTS:
            download.cancel()
            log(f"      ⛔ Refused '{fname}' — executable file type is not downloaded")
            return []

        out_path = self._unique_path(tender_dir, Path(fname).stem, ext)
        download.save_as(str(out_path))

        if out_path.stat().st_size == 0:
            out_path.unlink()
            raise RuntimeError(f"'{fname}' arrived empty")

        log(f"      💾 {out_path.name}")
        if ext.lower() == ".zip":
            return self._expand_zip(out_path, tender_dir, log)
        return [str(out_path)]

    @staticmethod
    def _unique_path(tender_dir: Path, stem: str, ext: str) -> Path:
        safe_stem = re.sub(r'[\\/*?:"<>|]', "_", stem)
        safe_stem = re.sub(r"\s+", "_", safe_stem)[:60].strip("_. ") or "document"
        safe_ext  = re.sub(r'[\\/*?:"<>|]', "_", ext)
        out_path  = tender_dir / f"{safe_stem}{safe_ext}"
        counter   = 1
        while out_path.exists():
            out_path = tender_dir / f"{safe_stem}_{counter}{safe_ext}"
            counter += 1
        return out_path

    def _expand_zip(self, zip_path: Path, tender_dir: Path, log) -> list:
        """Unpack one level of a ZIP, since file_reader can't read inside one.

        Member paths are flattened to a sanitized file name, so an entry can't
        write outside the tender folder. The ZIP is kept if it can't be unpacked.
        """
        extracted = []
        try:
            with zipfile.ZipFile(zip_path) as archive:
                members = [m for m in archive.infolist() if not m.is_dir()]
                if sum(m.file_size for m in members) > _MAX_ZIP_BYTES:
                    log(f"      ⚠️ {zip_path.name} is too large to unpack — kept as a ZIP")
                    return [str(zip_path)]
                for member in members:
                    member_path = Path(member.filename.replace("\\", "/"))
                    if member_path.suffix.lower() in _BLOCKED_EXTS:
                        log(f"      ⛔ Refused '{member_path.name}' inside {zip_path.name}")
                        continue
                    out_path = self._unique_path(tender_dir, member_path.stem, member_path.suffix[:10])
                    out_path.write_bytes(archive.read(member))
                    extracted.append(str(out_path))
        except (zipfile.BadZipFile, RuntimeError, OSError) as err:
            log(f"      ⚠️ Could not unpack {zip_path.name}: {err} — kept as a ZIP")
            for path in extracted:
                Path(path).unlink(missing_ok=True)
            return [str(zip_path)]
        zip_path.unlink()
        log(f"      📦 Extracted {len(extracted)} file(s) from {zip_path.name}")
        return extracted
