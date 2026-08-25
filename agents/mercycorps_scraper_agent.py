"""Mercy Corps tenders scraper — https://www.mercycorps.org/tenders

The tenders page is a plain server-rendered Drupal view: one GET returns the
complete markup, so this agent uses `requests` + regex with no Playwright.
Verified structure (2026-08):

    listing row:  <a href="/tenders/<slug>" class="c-button-box__link">
                    <h3 class="c-button-box__title">TITLE</h3></a>
                  <div class="c-field--name-field-tendering-office">…HQ…</div>
                  <div class="c-field--name-field-countries">…The Caribbean…</div>
                  <div class="c-field--name-field-date">
                    <time datetime="2026-08-19T21:16:12Z">…</time>   ← opens
                 to <time datetime="2026-09-19T00:00:00Z">…</time>   ← closes
    detail page:  <article class="node node--type-tender …"> holding the
                  description plus a "Files" field whose links are direct
                  document URLs under /sites/default/files/… (plain GET).

Everything Mercy Corps has open sits on that single page — there is no pager,
no search box and no filter, and closed tenders drop off the listing entirely.
There is therefore no publication-age window here (matching ISA): a handful of
tenders are posted a month, so anything still open for bids is collected and
the dedup tables guarantee each tender is reported exactly once.  The closing
date carried by the listing is used as a deadline gate on top of that, since a
tender can still be listed on the day it expires.
"""
import html as html_mod
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches, find_negative_keyword
from date_utils import is_deadline_active, IST

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}

# ── Listing ──────────────────────────────────────────────────────────────────
_ROW_LINK_RE = re.compile(
    r'<a\s+href="(?P<href>/tenders/[^"#?]+)"[^>]*class="[^"]*c-button-box__link[^"]*"[^>]*>'
    r'\s*<h3[^>]*class="[^"]*c-button-box__title[^"]*"[^>]*>(?P<title>.*?)</h3>',
    re.IGNORECASE | re.DOTALL,
)
_TIME_RE = re.compile(
    r'<time[^>]*datetime="([^"]+)"[^>]*>(.*?)</time>', re.IGNORECASE | re.DOTALL
)

# ── Detail ───────────────────────────────────────────────────────────────────
_ARTICLE_RE = re.compile(
    r'<article[^>]*node--type-tender[^>]*>(.*?)</article>', re.IGNORECASE | re.DOTALL
)
_H1_RE = re.compile(
    r'<h1[^>]*c-page-title__title[^>]*>(.*?)</h1>', re.IGNORECASE | re.DOTALL
)
_FILE_LINK_RE = re.compile(
    r'<a\s+href="([^"]+)"[^>]*class="[^"]*c-file__link[^"]*"[^>]*>\s*(.*?)</a>'
    r'(?:\s*<span[^>]*c-file__size[^>]*>\s*\(?([^)<]*)\)?\s*</span>)?',
    re.IGNORECASE | re.DOTALL,
)
# Fallback if Drupal ever renames the file field's classes.
_ANY_DOC_RE = re.compile(
    r'href="([^"]+\.(?:pdf|docx?|xlsx?|pptx?|zip|rar))(?:\?[^"]*)?"', re.IGNORECASE
)


def _strip_tags(fragment: str) -> str:
    """Turn an HTML fragment into readable plain text."""
    frag = re.sub(r'<(script|style)[\s\S]*?</\1>', ' ', fragment or '', flags=re.IGNORECASE)
    frag = re.sub(r'<br\s*/?>|</(p|div|li|h[1-6]|tr)>', '\n', frag, flags=re.IGNORECASE)
    frag = re.sub(r'<[^>]+>', ' ', frag)
    frag = html_mod.unescape(frag)
    frag = re.sub(r'[ \t\xa0]+', ' ', frag)
    frag = re.sub(r' *\n *', '\n', frag)
    return re.sub(r'\n{3,}', '\n\n', frag).strip()


def _field_text(block: str, field_name: str) -> str:
    """Pull the rendered value of a Drupal field (e.g. 'field-tendering-office')."""
    m = re.search(
        rf'c-field--name-{re.escape(field_name)}\b.*?c-field__content"?\s*>(.*?)</div>',
        block or '', re.IGNORECASE | re.DOTALL,
    )
    return _strip_tags(m.group(1)) if m else ""


def _ist_date_str(iso: str) -> str:
    """Convert the listing's UTC ISO timestamp to an IST 'YYYY-MM-DD' date.

    date_utils._DATE_FORMATS has no full-ISO pattern, so is_deadline_active()
    would fail open on the raw '2026-09-19T00:00:00Z' string and never gate
    anything.  Handing it a plain IST date keeps the comparison honest.
    """
    if not iso:
        return ""
    try:
        dt = datetime.strptime(iso.strip(), "%Y-%m-%dT%H:%M:%S%z")
    except ValueError:
        try:
            dt = datetime.fromisoformat(iso.strip().replace("Z", "+00:00"))
        except ValueError:
            return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST).strftime("%Y-%m-%d")


class MercyCorpsScraperAgent:
    BASE_URL = "https://www.mercycorps.org"
    LIST_URL = "https://www.mercycorps.org/tenders"

    def __init__(self):
        # One agent instance is reused across every keyword of a run (both by
        # api.py and cron_runner.py), so the listing is fetched once, not once
        # per keyword.
        self._cached_rows = None

    # ── listing ──────────────────────────────────────────────────────────────
    def _fetch_rows(self) -> list:
        resp = requests.get(self.LIST_URL, headers=_HEADERS, timeout=40)
        resp.raise_for_status()
        resp.encoding = "utf-8"          # Drupal serves UTF-8; don't let requests guess
        html = resp.text

        # The last row has no following anchor to bound it — stop at the end of
        # the document body so the footer's markup can't leak into its fields.
        tail = len(html)
        for marker in ("</main>", 'class="l-footer"', "<footer"):
            idx = html.find(marker)
            if idx != -1:
                tail = min(tail, idx)

        matches = list(_ROW_LINK_RE.finditer(html))
        rows = []
        for i, m in enumerate(matches):
            block_end = matches[i + 1].start() if i + 1 < len(matches) else tail
            block = html[m.end():max(m.end(), block_end)]

            title = _strip_tags(m.group("title"))
            if not title:
                continue

            times = _TIME_RE.findall(block)
            open_iso,  open_disp  = times[0] if len(times) > 0 else ("", "")
            close_iso, close_disp = times[1] if len(times) > 1 else ("", "")

            rows.append({
                "title":      title,
                "url":        urljoin(self.BASE_URL, m.group("href")),
                "office":     _field_text(block, "field-tendering-office"),
                "countries":  _field_text(block, "field-countries"),
                "open_iso":   open_iso,
                "open_disp":  _strip_tags(open_disp),
                "close_iso":  close_iso,
                "close_disp": _strip_tags(close_disp),
            })
        return rows

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
        log(f"🔍 [MERCYCORPS] Scanning for '{keyword}'...")

        if self._cached_rows is None:
            try:
                self._cached_rows = self._fetch_rows()
                log(f"   ↳ {len(self._cached_rows)} tender(s) listed on Mercy Corps")
            except Exception as e:
                log(f"❌ Mercy Corps listing fetch error: {e}")
                self._cached_rows = None
                return results
        else:
            log(f"   ↳ Using cached listing ({len(self._cached_rows)} tender(s))")

        rows = self._cached_rows
        base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "mercycorps"

        n_miss = n_neg = n_closed = n_dup = n_opened = n_err = 0

        for row in rows:
            try:
                title = row["title"]
                url   = row["url"]

                listing_text = " ".join(
                    p for p in (title, row["office"], row["countries"]) if p
                )

                # 1. Positive keyword — whole-word (keyword_utils), never substring
                if not keyword_matches(keyword, title, listing_text):
                    n_miss += 1
                    continue

                # 2. Negative keywords — stage 1, on what the listing shows
                neg = find_negative_keyword(title, team_id=team_id)
                if neg:
                    n_neg += 1
                    log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}' in title")
                    continue

                # 3. Deadline gate — a tender stays listed on its closing day
                close_date = _ist_date_str(row["close_iso"])
                if close_date and not is_deadline_active(close_date):
                    n_closed += 1
                    log(f"   ⏭ Skipping '{title[:60]}' — bidding closed on "
                        f"{row['close_disp'] or close_date}")
                    continue

                # 4. Dedup
                if db and db.is_duplicate(title, url, team_id=team_id):
                    n_dup += 1
                    log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                    continue

                n_opened += 1
                log(f"   📄 Opening: {title[:70]}")

                rec = self._extract_detail(
                    row, keyword, base, log, db=db, team_id=team_id
                )
                if rec:
                    results.append(rec)
                    if on_result_ready:
                        on_result_ready(rec)

            except Exception as row_err:
                n_err += 1
                log(f"   ⚠️ Row error: {row_err}")
                continue

        log(
            f"   📊 '{keyword}' summary on MERCYCORPS: {len(rows)} tender(s) listed → "
            f"{n_miss} without the keyword, "
            f"{n_neg} blocked by negative keywords, "
            f"{n_closed} archived/expired, "
            f"{n_dup} already collected, "
            f"{n_opened} opened for full check, {n_err} errored, {len(results)} saved"
        )

        return results

    # ── detail page ("Read More") ────────────────────────────────────────────
    def _extract_detail(self, row, keyword, base_dir, log, db=None, team_id="cnk"):
        url   = row["url"]
        title = row["title"]

        resp = requests.get(url, headers=_HEADERS, timeout=40)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        html = resp.text

        article = _ARTICLE_RE.search(html)
        body    = article.group(1) if article else html

        # Detail <h1> is the authoritative title (the listing truncates nothing
        # today, but keep them in sync if Drupal ever changes the view).
        h1 = _H1_RE.search(html)
        if h1:
            h1_text = _strip_tags(h1.group(1))
            if len(h1_text) > 5:
                title = h1_text

        body_text = _strip_tags(body)

        # Negative keywords — stage 2, now that the description is available
        neg = find_negative_keyword(title, body_text, team_id=team_id)
        if neg:
            log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on page")
            if db:
                db.mark_downloaded(title, url, "mercycorps", keyword, "", team_id=team_id)
            return None

        office    = _field_text(body, "field-tendering-office") or row["office"]
        countries = _field_text(body, "field-countries")        or row["countries"]

        times = _TIME_RE.findall(body)
        open_iso,  open_disp  = times[0] if len(times) > 0 else (row["open_iso"],  row["open_disp"])
        close_iso, close_disp = times[1] if len(times) > 1 else (row["close_iso"], row["close_disp"])
        open_disp  = _strip_tags(open_disp)
        close_disp = _strip_tags(close_disp)

        # ── Documents ────────────────────────────────────────────────────────
        docs, seen = [], set()
        for href, label, size in _FILE_LINK_RE.findall(body):
            abs_url = urljoin(self.BASE_URL, html_mod.unescape(href.strip()))
            if abs_url in seen:
                continue
            seen.add(abs_url)
            docs.append({
                "url":   abs_url,
                "label": _strip_tags(label) or Path(urlparse(abs_url).path).name,
                "size":  (size or "").strip(),
            })
        if not docs:
            for href in _ANY_DOC_RE.findall(body):
                abs_url = urljoin(self.BASE_URL, html_mod.unescape(href.strip()))
                if abs_url in seen:
                    continue
                seen.add(abs_url)
                docs.append({
                    "url":   abs_url,
                    "label": Path(urlparse(abs_url).path).name,
                    "size":  "",
                })

        log(f"      📎 {len(docs)} document(s) attached")

        safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
        safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
        tender_dir = base_dir / safe_kw / safe_title
        tender_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        doc_lines  = []
        for doc in docs:
            size_note = f" ({doc['size']})" if doc["size"] else ""
            doc_lines.append(f"{doc['label']}{size_note} — {doc['url']}")
            saved = self._download_file(doc["url"], tender_dir, doc["label"], log)
            if saved:
                downloaded.append(saved)

        # ── Page text for the summarizer ─────────────────────────────────────
        detail_lines = [
            f"Bid Title: {title}",
            "Inviting Authority: Mercy Corps",
        ]
        if office:
            detail_lines.append(f"Tendering Office: {office}")
        if countries:
            detail_lines.append(f"Country: {countries}")
        if open_disp:
            detail_lines.append(f"Publication Date: {open_disp}")
        if close_disp:
            detail_lines.append(f"Submission Deadline: {close_disp}")
        if body_text:
            detail_lines.append("")
            detail_lines.append(body_text)
        if doc_lines:
            detail_lines.append("")
            detail_lines.append("=== DOCUMENTS ===")
            detail_lines.extend(doc_lines)

        combined_text = "\n".join(detail_lines).strip()

        txt_path = tender_dir / "page_content.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(f"Source: {url}\n\n{combined_text}")

        if not downloaded:
            downloaded.append(str(txt_path))
            log("      📝 No documents — saved tender text only")
        else:
            log("      📝 Tender text also saved")

        published = open_disp or _ist_date_str(open_iso)
        deadline  = close_disp or _ist_date_str(close_iso)

        if db:
            db.mark_downloaded(
                title, url, "mercycorps", keyword, published or deadline, team_id=team_id
            )

        return {
            "keyword":        keyword,
            "title":          title,
            "url":            url,
            "page_text":      combined_text,
            "files":          downloaded,
            "tender_dir":     str(tender_dir),
            "site":           "mercycorps",
            "published_date": published,
            "deadline":       deadline,
        }

    # ── downloads ────────────────────────────────────────────────────────────
    def _download_file(self, url: str, tender_dir: Path,
                       preferred_name: str, log) -> "str | None":
        try:
            resp = requests.get(url, timeout=60, stream=True, headers=_HEADERS)
            resp.raise_for_status()

            if "text/html" in resp.headers.get("content-type", "").lower():
                log(f"      ⚠️ Server returned HTML for {url[-40:]} — skipping")
                return None

            # Extension: URL path first, Content-Disposition overrides it.
            ext = Path(urlparse(url).path).suffix[:10]
            cd  = resp.headers.get("content-disposition", "")
            if cd:
                m_cd = re.search(r'filename=["\']?([^"\';\r\n]+)["\']?', cd, re.IGNORECASE)
                if m_cd:
                    ext = Path(m_cd.group(1).strip()).suffix[:10] or ext
            if not ext:
                ext = ".pdf"
            if not ext.startswith("."):
                ext = "." + ext

            stem = re.sub(r'[\\/*?:"<>|]', "_", preferred_name or "")
            stem = re.sub(r"\s+", "_", stem)
            stem = re.sub(rf"{re.escape(ext)}$", "", stem, flags=re.IGNORECASE)
            stem = stem[:60].strip("_. ") or "document"

            out_path = tender_dir / f"{stem}{ext}"
            counter  = 1
            while out_path.exists():
                out_path = tender_dir / f"{stem}_{counter}{ext}"
                counter += 1

            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            log(f"      💾 {out_path.name}")
            return str(out_path)
        except Exception as e:
            log(f"      ⚠️ Download failed ({url[-60:]}): {e}")
            return None
