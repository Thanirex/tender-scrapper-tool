import re
import sys
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from date_utils import is_within_cutoff_ist, extract_date_from_text, get_max_age_hours, is_date_or_deadline_valid
from keyword_utils import keyword_matches, find_negative_keyword

# JS to collect RFP entries from the CHAI resource-center listing page.
# The page is pre-filtered to RFPs sorted by date desc via URL query params.
# WordPress posts: article elements with heading link + time[datetime].
# Returns: [{title, url, published}]
_JS_LIST = """
() => {
    const BASE = "https://www.clintonhealthaccess.org";
    const seen = new Set();
    const entries = [];

    // Try article/post card elements first (standard WordPress)
    const blocks = Array.from(document.querySelectorAll(
        'article, .resource-item, .post-item, .resource-card, .entry, li.resource'
    ));

    for (const el of blocks) {
        const linkEl = el.querySelector(
            'h1 a, h2 a, h3 a, h4 a, .entry-title a, .resource-title a, .post-title a'
        );
        if (!linkEl) continue;

        let href = linkEl.getAttribute('href') || '';
        if (!href) continue;
        if (href.startsWith('/')) href = BASE + href;
        if (!href.startsWith('http')) continue;
        if (seen.has(href)) continue;
        seen.add(href);

        const title = linkEl.textContent.trim();
        if (!title || title.length < 5) continue;

        // Prefer time[datetime] (ISO, unambiguous). CHAI doesn't use it,
        // so also scan the card's text for a visible date string.
        // Matches: "May 18, 2026" (US) or "18 May 2026" (EU).
        const timeEl = el.querySelector('time[datetime]');
        let published = timeEl ? (timeEl.getAttribute('datetime') || '') : '';
        if (!published) {
            const cardText = el.textContent || '';
            const dm = cardText.match(
                /\\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\\s+\\d{1,2},?\\s+\\d{4}|\\d{1,2}\\s+(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\\s+\\d{4})\\b/i
            );
            if (dm) published = dm[1].trim();
        }

        entries.push({ title, url: href, published });
    }

    // Fallback: scan all links that look like detail pages under this domain
    if (entries.length === 0) {
        document.querySelectorAll('a[href]').forEach(a => {
            let href = a.getAttribute('href') || '';
            if (!href) return;
            if (href.startsWith('/')) href = BASE + href;
            if (!href.startsWith(BASE)) return;
            // Skip the listing page itself and category/pagination links
            if (href.includes('?') || href.includes('#')) return;
            if (href === BASE + '/resource-center/' || href === BASE + '/resource-center') return;
            const title = a.textContent.trim();
            if (!title || title.length < 5) return;
            if (seen.has(href)) return;
            seen.add(href);
            entries.push({ title, url: href, published: '' });
        });
    }

    return entries;
}
"""


class CHAIScraperAgent:
    BASE_URL    = "https://www.clintonhealthaccess.org"
    LISTING_URL = (
        "https://www.clintonhealthaccess.org/resource-center/"
        "?_sft_category=rfp&sort_order=date+desc"
    )

    def __init__(self):
        self._cached_rows = None

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None, team_id="cnk", max_age_hours=None):
        if max_age_hours is None:
            max_age_hours = get_max_age_hours(team_id)
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [CHAI] Scanning for '{keyword}'...")

        if self._cached_rows is not None:
            rows = self._cached_rows
            log(f"   ↳ {len(rows)} tender row(s) (using cached listing)")
        else:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx     = browser.new_context(
                    accept_downloads=True,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                page = ctx.new_page()
                try:
                    page.goto(self.LISTING_URL, wait_until="domcontentloaded", timeout=45000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=12000)
                    except PlaywrightTimeout:
                        pass
                    page.wait_for_timeout(1500)

                    rows = page.evaluate(_JS_LIST) or []
                    self._cached_rows = rows
                    log(f"   ↳ {len(rows)} tender row(s) on listing page")
                except Exception as e:
                    log(f"❌ [CHAI] Fetch error: {e}")
                    rows = []
                finally:
                    browser.close()

        base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "chai"

        n_title_miss = n_neg = n_stale = n_dup = n_opened = 0
        for row in rows:
            title     = row["title"]
            url       = row["url"]
            published = row.get("published", "")

            # ── Keyword filter ──────────────────────────────────────
            if not keyword_matches(keyword, title):
                n_title_miss += 1
                continue

            neg = find_negative_keyword(title, team_id=team_id)
            if neg:
                n_neg += 1
                log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}' in title")
                continue

            # ── 24-hour publication check (listing datetime attr) ───
            if published:
                if not is_date_or_deadline_valid(published, max_age_hours):
                    n_stale += 1
                    log(f"   📅 Skipping '{title[:60]}' — date {published} expired")
                    continue
                log(f"   ✅ Date / Active Deadline OK: {published}")

            # ── Dedup check ─────────────────────────────────────────
            if db and db.is_duplicate(title, url, team_id=team_id):
                n_dup += 1
                log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                continue

            n_opened += 1
            log(f"   📄 Opening: {title[:70]}")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx     = browser.new_context(accept_downloads=True)
                try:
                    rec = self._extract_detail(ctx, url, keyword, title, published, base, log, db, max_age_hours=max_age_hours, team_id=team_id)
                    if rec:
                        results.append(rec)
                        if on_result_ready:
                            on_result_ready(rec)
                finally:
                    browser.close()

        log(
            f"   📊 '{keyword}' summary on CHAI: {len(rows)} RFP(s) listed → "
            f"{n_title_miss} without the keyword in the title, "
            f"{n_neg} blocked by negative keywords, {n_stale} older than {max_age_hours}h, "
            f"{n_dup} already collected, {n_opened} opened for full check, "
            f"{len(results)} saved"
        )
        return results

    # ── Detail page extraction ─────────────────────────────────────────────

    def _extract_detail(self, ctx, url, keyword, title, published, base_dir, log, db=None, max_age_hours: int = 24, team_id: str = "cnk"):
        detail = ctx.new_page()
        try:
            detail.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                detail.wait_for_load_state("networkidle", timeout=12000)
            except PlaywrightTimeout:
                pass
            detail.wait_for_timeout(1500)

            # Scroll to bottom to reveal any lazy-loaded download links
            detail.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            detail.wait_for_timeout(800)

            # ── Full page text ─────────────────────────────────────────────
            try:
                body_text = detail.locator("body").text_content() or ""
                body_text = re.sub(r"[ \t]+", " ", body_text)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            except Exception:
                body_text = ""

            neg = find_negative_keyword(title, body_text, team_id=team_id)
            if neg:
                log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on page")
                if db:
                    db.mark_downloaded(title, url, "chai", keyword, "", team_id=team_id)
                return None

            # ── Date check ────────────────────────────────────────────────
            # Use listing date if we got one; otherwise mine the page body.
            # extract_date_from_text() handles labeled formats ("Published: ...").
            # CHAI shows unlabeled "May 18, 2026" so we also try a bare US-date
            # regex as a final fallback before giving up.
            if not published and body_text:
                published = extract_date_from_text(body_text) or ""
            if not published and body_text:
                m = re.search(
                    r'\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|'
                    r'Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|'
                    r'Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4})',
                    body_text, re.IGNORECASE
                )
                if m:
                    published = m.group(1).strip()

            if published:
                if not is_date_or_deadline_valid(published, max_age_hours):
                    log(f"      📅 Skipping '{title[:60]}' — date {published} expired")
                    return None
                log(f"      ✅ Date / Active Deadline OK: {published}")
            else:
                log(f"      ⚠️ Could not find a publication date for '{title[:60]}' — skipping to be safe")
                return None

            # ── Collect document download links ────────────────────────────
            doc_links = detail.evaluate(f"""
            () => {{
                const BASE = "{self.BASE_URL}";
                const seen = new Set();
                const links = [];
                document.querySelectorAll('a[href]').forEach(a => {{
                    let h = a.href || '';
                    const t = a.textContent.trim();
                    if (!h || seen.has(h)) return;
                    if (h.startsWith('/')) h = BASE + h;

                    const isFile     = /\\.(pdf|docx?|xlsx?|zip)(\\?|$)/i.test(h);
                    const isWpUpload = /\\/wp-content\\/uploads\\//i.test(h);
                    const isDownload = /download|attachment|rfp|rfq|tender|document/i.test(t);

                    if (isFile || isWpUpload || isDownload) {{
                        seen.add(h);
                        links.push({{ href: h, text: t }});
                    }}
                }});
                return links;
            }}
            """) or []

            log(f"      📎 {len(doc_links)} document link(s) found")
            for d in doc_links:
                log(f"         → '{d['text'][:50]}' — {d['href'][-60:]}")

            safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
            safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
            tender_dir = base_dir / safe_kw / safe_title
            tender_dir.mkdir(parents=True, exist_ok=True)

            downloaded = []
            for doc in doc_links:
                saved = self._download_doc(doc["href"], tender_dir, log)
                if saved:
                    downloaded.append(saved)

            # Always save page text
            if body_text:
                txt_path = tender_dir / "page_content.txt"
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"Source: {url}\n\n{body_text}")
                if not downloaded:
                    downloaded.append(str(txt_path))
                    log(f"      📝 No documents — saved page text only")
                else:
                    log(f"      📝 Page text also saved")

            if db:
                db.mark_downloaded(title, url, "chai", keyword, "")

            return {
                "keyword":    keyword,
                "title":      title,
                "url":        url,
                "page_text":  body_text,
                "files":      downloaded,
                "tender_dir": str(tender_dir),
                "site":       "chai",
                "published":  published,
            }

        except Exception as e:
            log(f"      ❌ Extraction error: {e}")
            return None
        finally:
            try:
                detail.close()
            except Exception:
                pass

    # ── Download helper ────────────────────────────────────────────────────

    def _download_doc(self, url: str, tender_dir: Path, log) -> "str | None":
        try:
            resp = requests.get(
                url, timeout=60, stream=True, allow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/pdf,application/octet-stream,*/*",
                },
            )
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                log(f"      ⚠️ HTML response for {url[-50:]} — skipping")
                return None

            # Prefer Content-Disposition filename, fall back to URL segment
            fname = ""
            cd = resp.headers.get("content-disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', cd, re.IGNORECASE)
            if m:
                fname = m.group(1).strip().strip('"\'')
            if not fname:
                fname = url.rstrip("/").split("/")[-1].split("?")[0]
            if not fname or "." not in fname:
                ext = ".pdf" if "pdf" in content_type else ".zip" if "zip" in content_type else ".bin"
                fname = "document" + ext

            safe_stem = re.sub(r'[\\/*?:"<>|]', "_", Path(fname).stem)[:55]
            safe_ext  = Path(fname).suffix[:10] or ".pdf"
            out_path  = tender_dir / (safe_stem + safe_ext)

            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            log(f"      💾 {safe_stem + safe_ext}")
            return str(out_path)
        except Exception as e:
            log(f"      ⚠️ Download failed ({url[-60:]}): {e}")
            return None
