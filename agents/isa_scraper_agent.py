"""International Solar Alliance (ISA) procurement scraper — https://isa.int/procurement

The public page is an Angular SSR app.  Its "Filter by keyword or ref no." box is
a purely client-side filter over the ten notices currently rendered, and the tab
strip (RFP / EOI / Individual Contract / Archived) is plain <button>s with no
route — so driving that UI with a browser would silently miss anything not on the
visible page.  This agent therefore talks to the public JSON API that backs the
page instead: no auth, no cookies, no Playwright.

    GET https://isa.int/api/v1/procurement?status=active&page=1&limit=100
    -> {"data": {"items": [...], "meta": {"totalPages": N, ...}}}

Documents (tender document, corrigenda, pre-bid notice) are plain PDFs under
https://doc.isa.int/ and download with a normal requests GET.

Note that ISA's own `status=active` is an editorial flag, not a live-deadline
flag — several "active" notices already have a past endDate — so this agent
applies its own deadline gate.  There is no publication-age cutoff: ISA posts
only one or two notices a month, so anything still open for bids is collected and
the dedup tables guarantee each tender is reported exactly once.
"""
import re
import sys
import urllib.parse
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches, find_negative_keyword
from date_utils import is_deadline_active

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class ISAScraperAgent:
    API_URL    = "https://isa.int/api/v1/procurement"
    DOC_BASE   = "https://doc.isa.int/"
    PAGE_URL   = "https://isa.int/procurement/"
    PAGE_LIMIT = 100   # server hard-caps limit at 100 — 101+ returns HTTP 422

    def __init__(self):
        # One agent instance is reused across every keyword of a run (both by
        # api.py and cron_runner.py), so the listing is fetched once, not once
        # per keyword.
        self._cached_items = None

    # ── listing ──────────────────────────────────────────────────────────────
    def _fetch_all_notices(self, log) -> list:
        """Page through the procurement API and return every active notice."""
        items = []
        page  = 1
        while True:
            url = (
                f"{self.API_URL}?status=active"
                f"&page={page}&limit={self.PAGE_LIMIT}"
            )
            resp = requests.get(
                url,
                timeout=30,
                headers={"User-Agent": _UA, "Accept": "application/json"},
            )
            resp.raise_for_status()

            payload = resp.json().get("data") or {}
            batch   = payload.get("items") or []
            meta    = payload.get("meta") or {}
            try:
                total_pages = int(meta.get("totalPages") or 1)
            except (TypeError, ValueError):
                total_pages = 1

            items.extend(batch)
            log(f"   ↳ Page {page}/{total_pages}: {len(batch)} notice(s)")

            if not batch or page >= total_pages:
                break
            page += 1

        return items

    def _doc_url(self, path: str) -> str:
        """Turn an API document path into an absolute doc.isa.int URL."""
        path = (path or "").strip()
        if not path:
            return ""
        if path.lower().startswith(("http://", "https://")):
            return path
        return urllib.parse.urljoin(self.DOC_BASE, path.lstrip("/"))

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
        log(f"🔍 [ISA] Scanning for '{keyword}'...")

        if self._cached_items is None:
            try:
                self._cached_items = self._fetch_all_notices(log)
                log(f"   ↳ {len(self._cached_items)} active notice(s) on ISA procurement")
            except Exception as e:
                log(f"❌ ISA listing fetch error: {e}")
                self._cached_items = None
                return results
        else:
            log(f"   ↳ Using cached listing ({len(self._cached_items)} notice(s))")

        items = self._cached_items
        base  = Path(output_dir) if output_dir else DOWNLOADS_DIR / "isa"

        n_miss = n_neg = n_closed = n_dup = n_err = 0

        for item in items:
            try:
                title = (item.get("title") or "").strip()
                if not title:
                    continue

                ref_no      = (item.get("refNo") or "").strip()
                description = (item.get("tenderDescription") or "").strip()
                start_date  = (item.get("startDate") or "").strip()
                end_date    = (item.get("endDate") or "").strip()
                locations   = ", ".join(
                    (loc.get("name") or "").strip()
                    for loc in (item.get("locations") or [])
                    if isinstance(loc, dict)
                ).strip(", ")
                tender_type = (item.get("tenderType") or "").replace("_", " ").title()

                searchable = " ".join(
                    p for p in (title, ref_no, description, locations, tender_type) if p
                )

                # 1. Positive keyword — whole-word (keyword_utils), never substring
                if not keyword_matches(keyword, title, searchable):
                    n_miss += 1
                    continue

                # 2. Negative keywords
                neg = find_negative_keyword(title, searchable, team_id=team_id)
                if neg:
                    n_neg += 1
                    log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}'")
                    continue

                # 3. Deadline gate — ISA's "active" flag is editorial, not live
                if end_date and not is_deadline_active(end_date):
                    n_closed += 1
                    log(f"   ⏭ Skipping '{title[:60]}' — bidding closed on {end_date}")
                    continue

                # Unique per-tender URL: the listing page carries no detail route,
                # and a shared URL would make every tender after the first look
                # like a duplicate (is_duplicate matches on exact URL first).
                webpage_url = self.PAGE_URL
                if ref_no:
                    webpage_url = f"{self.PAGE_URL}?ref={urllib.parse.quote(ref_no, safe='')}"

                # 4. Dedup
                if db and db.is_duplicate(title, webpage_url, team_id=team_id):
                    n_dup += 1
                    log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                    continue

                log(f"   📄 {title[:70]}")

                safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
                safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
                tender_dir = base / safe_kw / safe_title
                tender_dir.mkdir(parents=True, exist_ok=True)

                # ── Documents ────────────────────────────────────────────────
                downloaded = []
                doc_lines  = []

                tender_doc_url = self._doc_url(item.get("tenderDocument"))
                if tender_doc_url:
                    doc_lines.append(f"Tender Document: {tender_doc_url}")
                    saved = self._download_file(
                        tender_doc_url, tender_dir, "Tender_Document", log
                    )
                    if saved:
                        downloaded.append(saved)
                else:
                    log("      ⚠️ No tender document attached to this notice")

                corrigenda = item.get("corrigendum") or []
                for idx, corr in enumerate(corrigenda, start=1):
                    if not isinstance(corr, dict):
                        continue
                    corr_url = self._doc_url(corr.get("file"))
                    if not corr_url:
                        continue
                    corr_type  = (corr.get("corrigendumType") or "corrigendum").replace("_", " ").title()
                    corr_title = (corr.get("title") or "").strip()
                    corr_date  = (corr.get("date") or "").strip()
                    doc_lines.append(
                        f"{corr_type} {idx}"
                        f"{f' ({corr_date})' if corr_date else ''}: "
                        f"{corr_title or corr_url} — {corr_url}"
                    )
                    saved = self._download_file(
                        corr_url,
                        tender_dir,
                        f"Corrigendum_{idx}_{corr_type}_{corr_title[:40]}".strip("_ "),
                        log,
                    )
                    if saved:
                        downloaded.append(saved)

                prebid_doc_url = self._doc_url(item.get("preBidMeetingNoticeDocument"))
                if prebid_doc_url:
                    doc_lines.append(f"Pre-Bid Notice: {prebid_doc_url}")
                    saved = self._download_file(
                        prebid_doc_url, tender_dir, "PreBid_Notice", log
                    )
                    if saved:
                        downloaded.append(saved)

                # ── Page text for the summarizer ─────────────────────────────
                prebid_date = (item.get("prebidDate") or "").strip()
                prebid_time = (item.get("prebidMeetingTime") or "").strip()
                prebid_link = (item.get("preBidMeetingLink") or "").strip()

                detail_lines = [
                    f"Bid Title: {title}",
                    f"Reference No: {ref_no}",
                    f"Tender Type: {tender_type}",
                    "Inviting Authority: International Solar Alliance (ISA)",
                    f"Country: {locations}",
                    f"Publication Date: {start_date}",
                    f"Submission Deadline: {end_date}",
                ]
                if prebid_date or prebid_time:
                    detail_lines.append(
                        f"Pre-Bid Meeting: {prebid_date} {prebid_time}".strip()
                    )
                if prebid_link:
                    detail_lines.append(f"Pre-Bid Meeting Link: {prebid_link}")
                if description:
                    detail_lines.append(f"Description: {description}")
                if doc_lines:
                    detail_lines.append("")
                    detail_lines.append("=== DOCUMENTS ===")
                    detail_lines.extend(doc_lines)

                combined_text = "\n".join(detail_lines).strip()

                txt_path = tender_dir / "page_content.txt"
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"Source: {webpage_url}\n\n{combined_text}")

                if not downloaded:
                    downloaded.append(str(txt_path))
                    log("      📝 No documents — saved notice text only")
                else:
                    log("      📝 Notice text also saved")

                if db:
                    db.mark_downloaded(
                        title, webpage_url, "isa", keyword,
                        start_date or end_date, team_id=team_id,
                    )

                rec = {
                    "keyword":        keyword,
                    "title":          title,
                    "url":            webpage_url,
                    "page_text":      combined_text,
                    "files":          downloaded,
                    "tender_dir":     str(tender_dir),
                    "site":           "isa",
                    "published_date": start_date or end_date,
                    "startDate":      start_date,
                    "endDate":        end_date,
                    "deadline":       end_date,
                }
                results.append(rec)
                if on_result_ready:
                    on_result_ready(rec)

            except Exception as row_err:
                n_err += 1
                log(f"   ⚠️ Row error: {row_err}")
                continue

        log(
            f"   📊 '{keyword}' summary on ISA: {len(items)} notice(s) listed → "
            f"{n_miss} without the keyword, "
            f"{n_neg} blocked by negative keywords, "
            f"{n_closed} archived/expired, "
            f"{n_dup} already collected, {n_err} errored, {len(results)} saved"
        )

        return results

    # ── downloads ────────────────────────────────────────────────────────────
    def _download_file(self, url: str, tender_dir: Path,
                       preferred_name: str, log) -> "str | None":
        try:
            resp = requests.get(
                url, timeout=45, stream=True, headers={"User-Agent": _UA}
            )
            resp.raise_for_status()

            if "text/html" in resp.headers.get("content-type", "").lower():
                log(f"      ⚠️ Server returned HTML for {url[-40:]} — skipping")
                return None

            # Extension: URL path first, Content-Disposition overrides it.
            ext = Path(urllib.parse.urlparse(url).path).suffix[:10]
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
            stem = re.sub(r"\s+", "_", stem)[:60].strip("_. ") or "document"

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
