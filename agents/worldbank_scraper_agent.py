import re
import sys
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches, find_negative_keyword

# Suppress InsecureRequestWarning for verify=False fallback
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_DOC_EXTENSIONS = (".pdf", ".docx", ".doc", ".xlsx", ".xls", ".zip", ".pptx")


class WorldBankScraperAgent:
    BASE_URL = "https://wbgeprocure-rfxnow.worldbank.org"
    LIST_URL = "https://wbgeprocure-rfxnow.worldbank.org/rfxnow/public/advertisement/index.html"

    def __init__(self):
        self._cached_items = None

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None, team_id="cnk"):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [World Bank] Scanning for '{keyword}'...")

        if self._cached_items is not None:
            items = self._cached_items
            log(f"   ↳ {len(items)} procurement listing(s) (using cached listing)")
        else:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(
                    accept_downloads=True,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                page = ctx.new_page()
                try:
                    page.goto(self.LIST_URL, wait_until="domcontentloaded", timeout=60000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=20000)
                    except PlaywrightTimeout:
                        pass
                    page.wait_for_timeout(3000)

                    items = self._collect_all_items(page, log)
                    self._cached_items = items
                    log(f"   ↳ {len(items)} procurement listing(s) found")
                except Exception as e:
                    log(f"❌ [World Bank] Listing fetch error: {e}")
                    items = []
                finally:
                    browser.close()

        base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "worldbank"
        skipped = 0
        n_neg = n_dup = n_opened = 0

        # We need a Playwright context for detail extraction if we aren't already in the loop
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(accept_downloads=True)
            try:
                for item in items:
                    title    = item["title"]
                    url      = item["url"]
                    raw_text = item.get("row_text", "")

                    # Keyword match against full row text (covers all columns)
                    searchable = raw_text if raw_text else title
                    if not keyword_matches(keyword, searchable):
                        skipped += 1
                        continue

                    neg = find_negative_keyword(title, searchable, team_id=team_id)
                    if neg:
                        n_neg += 1
                        log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}'")
                        continue

                    if db and db.is_duplicate(title, url, team_id=team_id):
                        n_dup += 1
                        log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                        continue

                    n_opened += 1
                    log(f"   📄 Opening: {title[:70]}")
                    rec = self._extract_detail(ctx, url, keyword, title, base, log, db)
                    if rec:
                        results.append(rec)
                        if on_result_ready:
                            on_result_ready(rec)

                log(
                    f"   📊 '{keyword}' summary on World Bank: {len(items)} listing(s) scanned → "
                    f"{skipped} without the keyword in the row text, "
                    f"{n_neg} blocked by negative keywords, {n_dup} already collected, "
                    f"{n_opened} opened for full check, {len(results)} saved"
                )

            except Exception as e:
                log(f"❌ World Bank scrape error: {e}")
            finally:
                browser.close()

        return results

    # ── Listing collection ─────────────────────────────────────────────────

    def _collect_all_items(self, page, log) -> list:
        """Walk all pages of the listing and return every item."""
        items    = []
        page_num = 1

        while True:
            page_items = self._collect_page_items(page, log)
            if not page_items:
                break

            # Deduplicate against already-collected URLs
            seen_urls = {i["url"] for i in items}
            new_items = [i for i in page_items if i["url"] not in seen_urls]
            if not new_items:
                break  # no new items — pagination stalled

            items.extend(new_items)
            log(f"   📄 Page {page_num}: {len(new_items)} item(s) collected (total {len(items)})")

            if not self._go_to_next_page(page):
                log(f"   ✅ Reached last page ({page_num})")
                break

            page_num += 1

        return items

    def _collect_page_items(self, page, log) -> list:
        """Extract procurement items from the currently loaded listing page."""
        # Primary: table rows — collect ALL links plus the full row text
        try:
            raw = page.eval_on_selector_all(
                "table tbody tr",
                """rows => {
                    const seen = new Set();
                    const res  = [];
                    rows.forEach(row => {
                        // Prefer the link whose text is the longest (most likely the title cell)
                        const links = Array.from(row.querySelectorAll('a[href]'));
                        if (!links.length) return;
                        const best = links.reduce((a, b) =>
                            b.textContent.trim().length > a.textContent.trim().length ? b : a
                        );
                        const href = best.href;
                        if (!href || seen.has(href)) return;
                        seen.add(href);
                        res.push({
                            title:    best.textContent.trim(),
                            href,
                            row_text: row.textContent.replace(/\\s+/g, ' ').trim().slice(0, 500)
                        });
                    });
                    return res;
                }"""
            )
            items = [
                {"title": r["title"], "url": r["href"], "row_text": r.get("row_text", "")}
                for r in raw
                if r and r.get("href")
            ]
            if items:
                return items
        except Exception:
            pass

        # Fallback: any link that looks like a detail/view page
        try:
            raw2 = page.eval_on_selector_all(
                "a[href]",
                """els => {
                    const seen = new Set();
                    const res  = [];
                    els.forEach(a => {
                        const h = a.href;
                        if (!h || seen.has(h)) return;
                        const t = a.textContent.trim();
                        if (!t || t.length < 4) return;
                        if (!/view|detail|adverti|rfx|tender|procure/i.test(h + t)) return;
                        // Grab the closest ancestor block for row_text
                        let node = a.parentElement;
                        for (let i = 0; i < 5 && node && node.tagName !== 'BODY'; i++) {
                            if (['TR','DIV','LI','ARTICLE'].includes(node.tagName)) break;
                            node = node.parentElement;
                        }
                        const row_text = node ? node.textContent.replace(/\\s+/g,' ').trim().slice(0,500) : '';
                        seen.add(h);
                        res.push({title: t, href: h, row_text});
                    });
                    return res;
                }"""
            )
            return [
                {"title": r["title"], "url": r["href"], "row_text": r.get("row_text", "")}
                for r in raw2
            ]
        except Exception as e:
            log(f"   ⚠️ Error reading listing page: {e}")
            return []

    def _go_to_next_page(self, page) -> bool:
        """Try to advance to the next page. Returns True if navigation happened."""
        # Strategy 1: aria-label attribute selectors (Angular Material, Bootstrap 4/5, etc.)
        aria_selectors = [
            "button[aria-label='Next page']",
            "button[aria-label='next page']",
            "button[aria-label='Next']",
            "button[aria-label='Go to next page']",
            "a[aria-label='Next page']",
            "a[aria-label='next page']",
            "a[aria-label='Next']",
            "a[aria-label='Go to next page']",
            ".pagination li.next:not(.disabled) a",
            ".pagination-next:not(.disabled) a",
            "a.page-link[rel='next']",
            "li.page-item.next:not(.disabled) a",
            "[class*='pagination'] [class*='next']:not([disabled]):not(.disabled)",
            "nav[aria-label*='pagination' i] button:not([disabled]):last-child",
        ]
        for sel in aria_selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=800) and btn.is_enabled():
                    btn.click()
                    page.wait_for_timeout(3000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeout:
                        pass
                    return True
            except Exception:
                continue

        # Strategy 2: text/icon based ("Next", ">", "›", "»")
        for text in ["Next", ">", "›", "»", "next"]:
            try:
                btn = page.locator(
                    f"button:text-is('{text}'):not([disabled]), "
                    f"a:text-is('{text}')"
                ).first
                if btn.is_visible(timeout=800) and btn.is_enabled():
                    btn.click()
                    page.wait_for_timeout(3000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeout:
                        pass
                    return True
            except Exception:
                continue

        # Strategy 3: Angular Material mat-paginator navigate-next button
        try:
            btn = page.locator(".mat-mdc-paginator-navigation-next, .mat-paginator-navigation-next").first
            if btn.is_visible(timeout=800) and btn.is_enabled():
                btn.click()
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass
                return True
        except Exception:
            pass

        return False

    # ── Detail extraction ──────────────────────────────────────────────────

    def _extract_detail(self, ctx, url: str, keyword: str, list_title: str,
                        base_dir: Path, log, db=None, team_id: str = "cnk") -> "dict | None":
        detail_page = ctx.new_page()
        try:
            detail_page.goto(url, wait_until="domcontentloaded", timeout=60000)
            try:
                detail_page.wait_for_load_state("networkidle", timeout=15000)
            except PlaywrightTimeout:
                pass
            detail_page.wait_for_timeout(2000)

            # Title: prefer h1/h2 over list title
            title = list_title
            for sel in ["h1", "h2", ".rfx-title", ".advertisement-title", "[class*='title']"]:
                try:
                    el = detail_page.locator(sel).first
                    t  = el.text_content(timeout=3000).strip()
                    if t and len(t) > 10 and t.lower() not in ("view", "details"):
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

            neg = find_negative_keyword(title, body_text, team_id=team_id)
            if neg:
                log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on page")
                if db:
                    db.mark_downloaded(title, url, "worldbank", keyword, "", team_id=team_id)
                return None

            # Collect document download URLs (template placeholders and .html routes filtered out)
            doc_hrefs = self._collect_doc_links(detail_page)

            log(f"      📋 {title[:70]}")
            log(f"      📎 {len(doc_hrefs)} resolvable document link(s) found")

            safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
            safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
            tender_dir = base_dir / safe_kw / safe_title
            tender_dir.mkdir(parents=True, exist_ok=True)

            # Try Playwright click-download first (handles Angular (click) handlers)
            downloaded = self._playwright_download_docs(detail_page, tender_dir, log)

            # Then try direct URL download for any remaining clean hrefs
            if doc_hrefs:
                cookies = {c["name"]: c["value"] for c in ctx.cookies()}
                for doc_url in doc_hrefs:
                    saved = self._download_doc(doc_url, tender_dir, log, cookies)
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
                db.mark_downloaded(title, url, "worldbank", keyword, "", team_id=team_id)

            return {
                "keyword":    keyword,
                "title":      title,
                "url":        url,
                "page_text":  body_text,
                "files":      downloaded,
                "tender_dir": str(tender_dir),
                "site":       "worldbank",
            }

        except Exception as e:
            log(f"      ❌ Extraction error on {url}: {e}")
            return None
        finally:
            try:
                detail_page.close()
            except Exception:
                pass

    def _collect_doc_links(self, page) -> list:
        """Find all document download links on the detail page."""
        try:
            hrefs = page.eval_on_selector_all(
                "a[href]",
                f"""els => {{
                    const exts = {list(_DOC_EXTENSIONS)};
                    const seen = new Set();
                    const res  = [];
                    els.forEach(a => {{
                        const h = a.href;
                        if (!h || seen.has(h)) return;
                        // Skip Angular template placeholders that haven't been bound yet
                        if (/%7B|%7D|\\{{|\\}}/i.test(h)) return;
                        const lower = h.toLowerCase();
                        // Skip SPA shell pages served as HTML routes
                        if (lower.endsWith('.html')) return;
                        const isDoc = exts.some(e => lower.endsWith(e))
                            || /download|attachment|document|file/i.test(lower);
                        if (!isDoc) return;
                        seen.add(h);
                        res.push(h);
                    }});
                    return res;
                }}"""
            )
            return [
                h for h in hrefs
                if not any(skip in h.lower() for skip in ["/static/", "/assets/", "/images/", "/css/", "/js/"])
            ]
        except Exception:
            return []

    def _download_doc(self, doc_url: str, tender_dir: Path, log, cookies: dict) -> "str | None":
        """Download a document via requests, returning saved path or None."""
        try:
            fname = doc_url.rstrip("/").split("/")[-1].split("?")[0]
            if not fname or "." not in fname:
                fname = "document.pdf"
            safe_stem = re.sub(r'[\\/*?:"<>|]', "_", Path(fname).stem)[:55]
            safe_ext  = Path(fname).suffix[:10] or ".pdf"
            out_path  = tender_dir / (safe_stem + safe_ext)

            resp = requests.get(
                doc_url, timeout=45, stream=True,
                cookies=cookies,
                headers={"User-Agent": "Mozilla/5.0"},
            )
            resp.raise_for_status()

            # Reject HTML responses regardless of the expected file extension
            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                log(f"      ⚠️ Server returned HTML for {fname} — skipping")
                return None

            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            log(f"      💾 {safe_stem + safe_ext}")
            return str(out_path)
        except Exception as dl_err:
            log(f"      ⚠️ Download failed ({doc_url}): {dl_err}")
            return None

    def _playwright_download_docs(self, page, tender_dir: Path, log) -> list:
        """Click download buttons and capture Playwright-triggered file downloads."""
        saved = []
        # Broad selector set — rfxnow uses Angular click handlers, not plain hrefs
        selector = (
            "button[class*='download'], a[class*='download'], "
            "[data-action*='download'], [ng-click*='download' i], "
            "[class*='btn']:has-text('Download'), [class*='btn']:has-text('download'), "
            "button:has-text('Download'), a:has-text('Download'), "
            "button:has-text('download'), a:has-text('download')"
        )
        try:
            btns = page.locator(selector).all()
            log(f"      🖱️ {len(btns)} download button(s) found via click-handler")
            for btn in btns[:15]:
                try:
                    with page.expect_download(timeout=20000) as dl_info:
                        btn.click()
                    download = dl_info.value
                    fname    = download.suggested_filename or "document.pdf"
                    safe     = re.sub(r'[\\/*?:"<>|]', "_", fname)[:80]
                    out_path = tender_dir / safe
                    download.save_as(str(out_path))
                    saved.append(str(out_path))
                    log(f"      💾 (click) {fname}")
                except Exception:
                    pass
        except Exception:
            pass
        return saved
