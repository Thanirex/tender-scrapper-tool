import re
import sys
import time
import html as html_mod
import requests
from pathlib import Path
from urllib.parse import urljoin

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from date_utils import is_within_24h_ist
from keyword_utils import keyword_matches, find_negative_keyword

# ReliefWeb job river pages are server-rendered (Drupal) — plain HTTP works,
# no Playwright needed.  The official API (api.reliefweb.int) v1 is
# decommissioned and v2 requires a pre-approved appname, so we scrape the
# HTML search results instead.  Verified structure (2026-07):
#   results:  <article class="... rw-river-article--job">, 20 per page
#   job link: https://reliefweb.int/job/<id>/<slug>
#   pager:    <li class="cd-pager__item--next"><a href="?search=...&page=N">
#   detail:   <h1> title, <dl> with <dt>Posted / Closing date</dt> whose
#             <dd> holds <time datetime="ISO">; posted has a real timestamp,
#             closing is always midnight.

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    )
}


class ReliefWebScraperAgent:
    BASE_URL = "https://reliefweb.int"
    LIST_URL = "https://reliefweb.int/jobs"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [ReliefWeb] Searching jobs for '{keyword}' — walking every result page; "
            f"only postings from the last 24 hours are collected")

        base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "reliefweb"
        seen_urls:  set = set()
        seen_pages: set = set()
        page_num = 1

        # Per-keyword tallies so the closing summary can explain exactly
        # where every listed job went instead of ending in silence.
        n_checked = n_title_miss = n_neg = n_stale = n_dup = n_opened = 0

        try:
            resp = requests.get(self.LIST_URL, params={"search": keyword},
                                headers=_HEADERS, timeout=45)
            resp.raise_for_status()
        except Exception as e:
            log(f"❌ ReliefWeb search failed: {e}")
            return results
        page_html = resp.text

        while True:
            cards = self._parse_cards(page_html)
            log(f"   📄 Page {page_num}: {len(cards)} job(s) listed")
            if not cards:
                break

            for card in cards:
                title = card["title"]
                url   = card["url"]
                if not title or not url or url in seen_urls:
                    continue
                seen_urls.add(url)
                n_checked += 1

                if not keyword_matches(keyword, title):
                    n_title_miss += 1
                    continue

                neg = find_negative_keyword(title)
                if neg:
                    n_neg += 1
                    log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}' in title")
                    continue

                # Listing pre-filter: skip stale posts without fetching the
                # detail page.  Only trusted when the card exposes a real
                # (non-midnight) timestamp — the detail page re-checks anyway.
                listed = card.get("posted", "")
                if listed and not is_within_24h_ist(listed):
                    n_stale += 1
                    log(f"   📅 Skipping '{title[:60]}' — matched, but posted {listed[:10]} (>24h ago)")
                    continue

                if db and db.is_duplicate(title, url):
                    n_dup += 1
                    log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                    continue

                n_opened += 1
                log(f"   📄 Opening: {title[:70]}")
                rec = self._extract_detail(url, keyword, title, base, log, db)
                if rec:
                    results.append(rec)
                    if on_result_ready:
                        on_result_ready(rec)
                time.sleep(0.5)   # politeness delay between detail fetches

            next_url = self._find_next_page(page_html)
            if not next_url:
                log(f"   ✅ Reached last page ({page_num})")
                break
            if next_url in seen_pages:
                log(f"   ✅ Pager repeated a page — stopping at page {page_num}")
                break
            seen_pages.add(next_url)

            page_num += 1
            try:
                time.sleep(0.5)
                resp = requests.get(next_url, headers=_HEADERS, timeout=45)
                resp.raise_for_status()
                page_html = resp.text
            except Exception as e:
                log(f"   ⚠️ Could not load page {page_num}: {e}")
                break

        # ── Closing summary: account for every job that was listed ──────────
        log(
            f"   📊 '{keyword}' summary: {n_checked} job(s) scanned across "
            f"{page_num} page(s) → {n_title_miss} without '{keyword}' in the title, "
            f"{n_stale} matched but older than 24h, {n_dup} already collected, "
            f"{n_neg} blocked by negative keywords, {n_opened} fully checked, "
            f"{len(results)} saved"
        )
        if not results and n_stale:
            log(f"   ℹ️ Every keyword match was posted more than 24 hours ago — "
                f"nothing new since the last run.")

        return results

    # ── Listing parsing ─────────────────────────────────────────────────────

    def _parse_cards(self, page_html: str) -> list:
        """Extract {title, url, posted} from each job card on a results page."""
        cards = []
        for block in re.findall(
            r"<article[^>]*rw-river-article--job.*?</article>",
            page_html, re.DOTALL,
        ):
            m = re.search(
                r'href="(https://reliefweb\.int/job/[^"]+)"[^>]*>(.*?)</a>',
                block, re.DOTALL,
            )
            if not m:
                continue
            url   = html_mod.unescape(m.group(1))
            title = html_mod.unescape(re.sub(r"<[^>]+>", " ", m.group(2)))
            title = re.sub(r"\s+", " ", title).strip()

            # Posted has a real timestamp; the closing date is always
            # rendered as midnight.  If no non-midnight time exists, leave
            # empty so the detail page decides.
            posted = ""
            for dt in re.findall(r'datetime="([^"]+)"', block):
                if "T00:00:00" not in dt:
                    posted = dt
                    break

            cards.append({"title": title, "url": url, "posted": posted})
        return cards

    def _find_next_page(self, page_html: str) -> "str | None":
        m = re.search(
            r'cd-pager__item--next[^>]*>\s*<a[^>]*href="([^"]+)"',
            page_html, re.DOTALL,
        )
        if not m:
            m = re.search(r'<a[^>]*rel="next"[^>]*href="([^"]+)"', page_html)
        if not m:
            return None
        return urljoin(self.LIST_URL, html_mod.unescape(m.group(1)))

    # ── Detail page ─────────────────────────────────────────────────────────

    def _extract_detail(self, url, keyword, list_title, base_dir, log, db=None):
        try:
            resp = requests.get(url, headers=_HEADERS, timeout=45)
            resp.raise_for_status()
        except Exception as e:
            log(f"      ⚠️ Could not load job page: {e}")
            return None
        page = resp.text

        m = re.search(r"<h1[^>]*>(.*?)</h1>", page, re.DOTALL)
        if m:
            title = html_mod.unescape(re.sub(r"<[^>]+>", " ", m.group(1)))
            title = re.sub(r"\s+", " ", title).strip() or list_title
        else:
            title = list_title

        posted  = self._dt_field(page, "Posted")
        closing = self._dt_field(page, "Closing date")

        # ── 24-hour publication check (authoritative) ────────────────────
        if posted:
            if not is_within_24h_ist(posted):
                log(f"      📅 Skipping '{title[:60]}' — posted {posted[:10]} (>24h ago)")
                return None
        else:
            log(f"      ⚠️ No posted date found for '{title[:60]}' — skipping")
            return None

        body_text = self._page_text(page)

        neg = find_negative_keyword(title, body_text)
        if neg:
            log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on page")
            if db:
                db.mark_downloaded(title, url, "reliefweb", keyword, posted)
            return None

        if db and db.is_duplicate(title, url):
            log(f"      ⏩ Duplicate: {title[:60]}")
            return None

        # ReliefWeb jobs rarely attach documents — the page content itself
        # is the tender, saved as text for the summarizer and the ZIP.
        safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
        safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
        tender_dir = base_dir / safe_kw / safe_title
        tender_dir.mkdir(parents=True, exist_ok=True)

        txt_path = tender_dir / "page_content.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(
                f"Source: {url}\nPosted: {posted}\nClosing date: {closing}\n\n{body_text}"
            )
        log(f"      📝 Page content saved ({len(body_text):,} chars)")

        if db:
            db.mark_downloaded(title, url, "reliefweb", keyword, posted)

        return {
            "keyword":    keyword,
            "title":      title,
            "url":        url,
            "page_text":  body_text,
            "files":      [str(txt_path)],
            "tender_dir": str(tender_dir),
            "site":       "reliefweb",
            "published":  posted,
            "deadline":   closing,
        }

    def _dt_field(self, page: str, label: str) -> str:
        """Read the ISO datetime for a <dt>label</dt> row (Posted, Closing date)."""
        m = re.search(
            rf'<dt[^>]*>\s*{re.escape(label)}\s*</dt>\s*'
            rf'<dd[^>]*>(?:(?!</dd>).)*?datetime="([^"]+)"',
            page, re.DOTALL | re.IGNORECASE,
        )
        return m.group(1) if m else ""

    def _page_text(self, page: str) -> str:
        """Visible text of the job page (main region, tags stripped)."""
        m = re.search(r"<main\b.*?</main>", page, re.DOTALL | re.IGNORECASE)
        region = m.group(0) if m else page
        region = re.sub(r"<(script|style|noscript|svg)\b.*?</\1>", " ",
                        region, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r"<br\s*/?>|</(?:p|div|li|dd|dt|h[1-6]|section|article)>",
                      "\n", region, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", " ", text)
        text = html_mod.unescape(text)
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r" ?\n ?", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
