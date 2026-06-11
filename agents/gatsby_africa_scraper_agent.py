import re
import sys
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR


class GatsbyAfricaScraperAgent:
    BASE_URL    = "https://www.gatsbyafrica.org.uk"
    LISTING_URL = "https://www.gatsbyafrica.org.uk/tender/"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [Gatsby Africa] Scanning for '{keyword}'...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx  = browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            try:
                tenders = self._find_tenders(page, keyword, log)
                log(f"   ↳ {len(tenders)} tender(s) matched '{keyword}'")

                base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "gatsbyafrica"

                for tender in tenders:
                    title = tender["title"]
                    url   = tender["url"]

                    if db and db.is_duplicate(title, url):
                        log(f"   ⏩ Duplicate: {title[:60]}")
                        continue

                    log(f"   📄 {title[:70]}")
                    rec = self._extract_detail(ctx, url, keyword, title, base, log, db)
                    if rec:
                        results.append(rec)
                        if on_result_ready:
                            on_result_ready(rec)

            except Exception as e:
                log(f"❌ Gatsby Africa scrape error: {e}")
            finally:
                browser.close()

        return results

    # ── Search strategies ──────────────────────────────────────────────────

    def _find_tenders(self, page, keyword, log) -> list:
        """
        Try three strategies in order:
        1. WordPress search URL  (?s=keyword&post_type=tender)
        2. Playwright form interaction on the listing page
        3. Load-all fallback — collect every tender link and filter client-side
        """

        # Strategy 1: URL-based WP search
        tenders = self._url_search(page, keyword, log)
        if tenders:   # None = search unavailable, [] = no matches → fall through
            return tenders

        # Strategy 2: Playwright form interaction
        tenders = self._form_search(page, keyword, log)
        if tenders:
            return tenders

        # Strategy 3: Load all tender links and filter
        return self._listing_filter(page, keyword, log)

    def _url_search(self, page, keyword, log) -> "list | None":
        """Try WP search URL; return None if the server 404s/errors."""
        search_url = f"{self.BASE_URL}/?s={requests.utils.quote(keyword)}&post_type=tender"
        try:
            resp = page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            if resp and resp.status == 404:
                return None
            page.wait_for_timeout(2000)

            # If WP auto-redirected to a single tender page, we're done
            if "/tender/" in page.url and page.url.rstrip("/") != self.LISTING_URL.rstrip("/"):
                title = self._get_page_title(page)
                log(f"   🔎 WP search redirected to single result: {title[:60]}")
                return [{"title": title, "url": page.url}]

            links = self._scrape_tender_links(page, keyword)
            if links:
                log(f"   🔎 WP search URL returned {len(links)} result(s)")
            else:
                log(f"   ℹ️ WP search URL returned 0 results")
            return links
        except Exception:
            return None

    def _form_search(self, page, keyword, log) -> "list | None":
        """Find the search input on the listing page and submit it."""
        try:
            page.goto(self.LISTING_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
        except Exception:
            return None

        search_selectors = [
            "input[type='search']",
            "input[name='s']",
            "input[placeholder*='Search' i]",
            "input[placeholder*='search' i]",
            "[class*='search'] input[type='text']",
            "#search-field",
            ".search-input",
        ]
        for sel in search_selectors:
            try:
                inp = page.locator(sel).first
                if inp.is_visible(timeout=1000):
                    inp.fill(keyword)
                    inp.press("Enter")
                    page.wait_for_timeout(2500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except PlaywrightTimeout:
                        pass
                    links = self._scrape_tender_links(page, keyword)
                    log(f"   🔎 Form search returned {len(links)} result(s)")
                    return links
            except Exception:
                continue
        return None

    def _listing_filter(self, page, keyword, log) -> list:
        """Load the tender archive and filter all links by keyword in title."""
        try:
            page.goto(self.LISTING_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1500)
        except Exception:
            return []
        links = self._scrape_tender_links(page, keyword)
        log(f"   🔎 Listing page filter returned {len(links)} match(es)")
        return links

    def _scrape_tender_links(self, page, keyword) -> list:
        """Collect tender detail links whose title contains the keyword."""
        try:
            all_links = page.eval_on_selector_all(
                "a[href*='/tender/']",
                """els => {
                    const seen = new Set();
                    const res  = [];
                    els.forEach(a => {
                        const h = a.href;
                        // Skip the archive root itself
                        if (!h || h.replace(/\\/$/,'').endsWith('/tender')) return;
                        if (seen.has(h)) return;
                        seen.add(h);
                        // Prefer the link's own text; fall back to title attribute
                        const txt = a.textContent.trim() || a.getAttribute('title') || '';
                        // Also collect the closest heading/paragraph ancestor text
                        let ctxText = '';
                        let node = a.parentElement;
                        for (let i = 0; i < 5 && node && node.tagName !== 'BODY'; i++) {
                            const h2 = node.querySelector('h2,h3,h4');
                            if (h2) { ctxText = h2.textContent.trim(); break; }
                            node = node.parentElement;
                        }
                        res.push({ title: txt || ctxText, url: h, ctxTitle: ctxText });
                    });
                    return res;
                }"""
            )
            kw = keyword.lower()
            return [
                {"title": t["ctxTitle"] or t["title"], "url": t["url"]}
                for t in all_links
                if kw in (t["title"] + " " + t["ctxTitle"] + " " + t["url"]).lower()
            ]
        except Exception:
            return []

    # ── Detail page extraction ─────────────────────────────────────────────

    def _extract_detail(self, ctx, url, keyword, list_title, base_dir, log, db=None):
        detail_page = ctx.new_page()
        try:
            detail_page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                detail_page.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            detail_page.wait_for_timeout(1500)

            # Title
            title = list_title
            for sel in ["h1", ".entry-title", "h2.tender-title", "h2"]:
                try:
                    t = detail_page.locator(sel).first.text_content(timeout=3000).strip()
                    if t and len(t) > 5:
                        title = t
                        break
                except Exception:
                    pass

            # Full page text
            try:
                body_text = detail_page.locator("body").text_content()
                body_text = re.sub(r"[ \t]+", " ", body_text)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            except Exception:
                body_text = ""

            # All document links — PDFs from /app/uploads/ and any "RFP/download/view" links
            doc_links = detail_page.eval_on_selector_all(
                "a[href]",
                """els => {
                    const seen = new Set();
                    return els
                        .filter(a => {
                            const h = a.href;
                            const t = a.textContent.trim().toLowerCase();
                            if (!h || seen.has(h)) return false;
                            const isDoc = h.toLowerCase().endsWith('.pdf')
                                || /\\/app\\/uploads\\//i.test(h)
                                || /rfp|download|view.*full|protocol|survey|document/i.test(t);
                            if (!isDoc) return false;
                            seen.add(h);
                            return true;
                        })
                        .map(a => ({ href: a.href, text: a.textContent.trim() }));
                }"""
            )

            log(f"      📋 {title[:70]}")
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

            # Save page text
            if body_text:
                txt_path = tender_dir / "page_content.txt"
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"Source: {url}\n\n{body_text}")
                if not downloaded:
                    downloaded.append(str(txt_path))
                    log(f"      📝 No PDFs — saved page text only")
                else:
                    log(f"      📝 Page text also saved")

            if db:
                db.mark_downloaded(title, url, "gatsbyafrica", keyword, "")

            return {
                "keyword":    keyword,
                "title":      title,
                "url":        url,
                "page_text":  body_text,
                "files":      downloaded,
                "tender_dir": str(tender_dir),
                "site":       "gatsbyafrica",
            }

        except Exception as e:
            log(f"      ❌ Extraction error: {e}")
            return None
        finally:
            try:
                detail_page.close()
            except Exception:
                pass

    # ── Helpers ────────────────────────────────────────────────────────────

    def _get_page_title(self, page) -> str:
        for sel in ["h1", ".entry-title", "h2"]:
            try:
                t = page.locator(sel).first.text_content(timeout=2000).strip()
                if t:
                    return t
            except Exception:
                pass
        return page.title()

    def _download_doc(self, url: str, tender_dir: Path, log) -> "str | None":
        try:
            fname     = url.rstrip("/").split("/")[-1].split("?")[0]
            if not fname or "." not in fname:
                fname = "document.pdf"
            safe_stem = re.sub(r'[\\/*?:"<>|]', "_", Path(fname).stem)[:55]
            safe_ext  = Path(fname).suffix[:10] or ".pdf"
            out_path  = tender_dir / (safe_stem + safe_ext)

            resp = requests.get(url, timeout=45, stream=True,
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
            log(f"      ⚠️ Download failed ({url}): {e}")
            return None
