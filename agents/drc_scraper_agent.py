import re
import sys
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from date_utils import is_within_24h_ist

# JS to scrape all tender entries from the DRC listing page.
# Page uses <article class="tenderList__item"> with data-* attributes
# and <time datetime="YYYY-MM-DD"> elements — NOT a <table>.
# Returns: [{title, url, published, deadline, status}]
_JS_LIST = """
() => {
    const BASE = "https://drc.ngo";
    const articles = Array.from(document.querySelectorAll('article.tenderList__item'));
    return articles.map(a => {
        const titleEl  = a.querySelector('span.tenderList__item__title');
        const link     = a.querySelector('a.casper[href]');
        const pubTime  = a.querySelector('span.tenderList__item__published time[datetime]');
        const deadTime = a.querySelector('span.tenderList__item__deadline time[datetime]');

        const title  = titleEl ? titleEl.textContent.trim() : (a.dataset.title || '');
        if (!title) return null;

        let href = link ? link.getAttribute('href') : '';
        if (href && href.startsWith('/')) href = BASE + href;

        // Use datetime attr (YYYY-MM-DD) — compatible with date_utils.is_within_24h_ist
        const published = pubTime  ? pubTime.getAttribute('datetime')  : (a.dataset.published || '');
        const deadline  = deadTime ? deadTime.getAttribute('datetime') : (a.dataset.deadline  || '');
        const status    = (a.dataset.status || '').toLowerCase();

        return { title, url: href, published, deadline, status };
    }).filter(Boolean);
}
"""


class DRCScraperAgent:
    BASE_URL    = "https://drc.ngo"
    LISTING_URL = "https://drc.ngo/en/tenders/"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [DRC] Scanning for '{keyword}'...")

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
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass
                page.wait_for_timeout(1500)

                rows = page.evaluate(_JS_LIST) or []
                log(f"   ↳ {len(rows)} tender row(s) on listing page")

                kw_lower = keyword.lower()
                base     = Path(output_dir) if output_dir else DOWNLOADS_DIR / "drc"

                for row in rows:
                    title     = row["title"]
                    url       = row["url"]
                    published = row["published"]
                    deadline  = row["deadline"]
                    status    = row.get("status", "")

                    # ── Keyword filter ────────────────────────────────────────
                    if kw_lower not in title.lower():
                        continue

                    # ── Active check — skip archived/expired tenders ──────────
                    if status in ("archived", "expired", "closed"):
                        log(f"   ⏭ {status.capitalize()}: {title[:60]}")
                        continue

                    # ── 24-hour publication check ─────────────────────────────
                    if published:
                        if not is_within_24h_ist(published):
                            log(f"   📅 Skipping '{title[:60]}' — published {published} (>24h ago)")
                            continue
                    else:
                        log(f"   ⚠️ No published date for '{title[:60]}' — processing anyway")

                    # ── Dedup check ───────────────────────────────────────────
                    if db and db.is_duplicate(title, url):
                        log(f"   ⏩ Duplicate: {title[:60]}")
                        continue

                    log(f"   📄 {title[:70]}")
                    rec = self._extract_detail(ctx, url, keyword, title, published, deadline, base, log, db)
                    if rec:
                        results.append(rec)
                        if on_result_ready:
                            on_result_ready(rec)

            except Exception as e:
                log(f"❌ DRC scrape error: {e}")
            finally:
                browser.close()

        return results

    # ── Detail page extraction ─────────────────────────────────────────────

    def _extract_detail(self, ctx, url, keyword, title, published, deadline, base_dir, log, db=None):
        detail = ctx.new_page()
        try:
            detail.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                detail.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            detail.wait_for_timeout(1500)

            # Confirm not expired via page text
            try:
                body_text = detail.locator("body").text_content() or ""
                if "this tender is expired" in body_text.lower():
                    log(f"      ⏭ Detail page says expired: {title[:60]}")
                    return None
            except Exception:
                body_text = ""

            body_text = re.sub(r"[ \t]+", " ", body_text)
            body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()

            # Collect document links from the copy section
            doc_links = detail.evaluate(f"""
            () => {{
                const BASE = "{self.BASE_URL}";
                const copy = (
                    document.querySelector('.tenderPage__copy') ||
                    document.querySelector('main') ||
                    document.body
                );
                const seen = new Set();
                const links = [];
                copy.querySelectorAll('a[href]').forEach(a => {{
                    let h = a.href || '';
                    // Also handle relative /media/... paths
                    if (h.startsWith('/')) h = BASE + h;
                    if (!h || seen.has(h)) return;
                    // Accept /media/ paths and direct file extensions
                    const isMedia = /\\/media\\//i.test(h);
                    const isFile  = /\\.(pdf|docx?|xlsx?|zip)(\\?|$)/i.test(h);
                    if (isMedia || isFile) {{
                        seen.add(h);
                        links.push({{ href: h, text: a.textContent.trim() }});
                    }}
                }});
                return links;
            }}
            """)

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
                db.mark_downloaded(title, url, "drc", keyword, deadline)

            return {
                "keyword":    keyword,
                "title":      title,
                "url":        url,
                "page_text":  body_text,
                "files":      downloaded,
                "tender_dir": str(tender_dir),
                "site":       "drc",
                "deadline":   deadline,
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
            fname     = url.rstrip("/").split("/")[-1].split("?")[0]
            if not fname or "." not in fname:
                fname = "document.bin"
            safe_stem = re.sub(r'[\\/*?:"<>|]', "_", Path(fname).stem)[:55]
            safe_ext  = Path(fname).suffix[:10] or ".bin"
            out_path  = tender_dir / (safe_stem + safe_ext)

            resp = requests.get(url, timeout=60, stream=True,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()

            if "text/html" in resp.headers.get("content-type", ""):
                log(f"      ⚠️ HTML response for {fname} — skipping")
                return None

            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            log(f"      💾 {safe_stem + safe_ext}")
            return str(out_path)
        except Exception as e:
            log(f"      ⚠️ Download failed ({url[-60:]}): {e}")
            return None
