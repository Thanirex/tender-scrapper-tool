import re
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches


class ACBFScraperAgent:
    PAGE_URL = "https://theacbf.org/join-us/procurement-and-consultancies/"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [ACBF] Scanning for '{keyword}'...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context()
            page = ctx.new_page()
            try:
                page.goto(self.PAGE_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)

                items = self._collect_items(page, log)
                log(f"   ↳ {len(items)} procurement item(s) found")

                for item in items:
                    title = item["title"]
                    url   = item["url"]

                    if not title or not url:
                        continue

                    if not keyword_matches(keyword, title):
                        continue

                    if db and db.is_duplicate(title, url):
                        log(f"   ⏩ Duplicate: {title[:60]}")
                        continue

                    log(f"   📄 {title[:70]}")
                    base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "acbf"
                    rec = self._extract_detail(ctx, url, keyword, title, item.get("deadline", ""), base, log, db)
                    if rec:
                        results.append(rec)
                        if on_result_ready:
                            on_result_ready(rec)

            except Exception as e:
                log(f"❌ ACBF scrape error: {e}")
            finally:
                browser.close()

        return results

    def _collect_items(self, page, log) -> list:
        """Collect all unique consultancy links, walking ancestors for title/date."""
        try:
            items = page.eval_on_selector_all(
                "a[href*='/consultancies/']",
                """els => {
                    const seen = new Set();
                    const results = [];
                    els.forEach(a => {
                        const href = a.href;
                        if (!href || seen.has(href)) return;
                        if (!/\\/consultancies\\/[^#?]+/.test(href)) return;
                        seen.add(href);

                        // Walk up ancestors (max 8 levels) to find a heading and meta <p>
                        let heading = null, meta = '';
                        let node = a.parentElement;
                        for (let i = 0; i < 8 && node && node.tagName !== 'BODY'; i++) {
                            if (!heading) {
                                const h = node.querySelector('h1,h2,h3,h4,h5');
                                if (h) heading = h;
                            }
                            if (!meta) {
                                const p = node.querySelector('p');
                                if (p) meta = p.textContent.trim();
                            }
                            if (heading && meta) break;
                            node = node.parentElement;
                        }

                        // Fall back to slug-derived title if no heading found
                        const slug = href.replace(/\\/$/, '').split('/').pop() || '';
                        const slugTitle = slug.replace(/-/g, ' ');
                        const dm = meta.match(/date[:\\s]+([^|\\n]+)/i);

                        results.push({
                            title:    heading ? heading.textContent.trim() : slugTitle,
                            deadline: dm ? dm[1].trim() : '',
                            href
                        });
                    });
                    return results;
                }"""
            )
        except Exception as e:
            log(f"   ⚠️ Error reading item list: {e}")
            return []

        return [{"title": i["title"], "url": i["href"], "deadline": i["deadline"]} for i in items]

    def _extract_detail(self, ctx, url: str, keyword: str, list_title: str,
                        deadline: str, base_dir: Path, log, db=None) -> dict | None:
        detail_page = ctx.new_page()
        try:
            detail_page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                detail_page.locator("h1").wait_for(state="visible", timeout=10000)
            except PlaywrightTimeout:
                log(f"      ⚠️ Page did not render — skipping")
                return None

            try:
                title = detail_page.locator("h1").first.text_content().strip()
            except Exception:
                title = list_title

            try:
                body_text = detail_page.locator("body").text_content()
                body_text = re.sub(r"[ \t]+", " ", body_text)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            except Exception:
                body_text = ""

            log(f"      📋 {title[:70]}")

            safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
            safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
            tender_dir = base_dir / safe_kw / safe_title
            tender_dir.mkdir(parents=True, exist_ok=True)

            saved = []
            if body_text:
                txt_path = tender_dir / "page_content.txt"
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"Source: {url}\n\n{body_text}")
                saved.append(str(txt_path))
                log(f"      📝 Page content saved ({len(body_text):,} chars)")

            if db:
                db.mark_downloaded(title, url, "acbf", keyword, deadline)

            return {
                "keyword":    keyword,
                "title":      title,
                "url":        url,
                "page_text":  body_text,
                "files":      saved,
                "tender_dir": str(tender_dir),
                "deadline":   deadline,
                "site":       "acbf",
            }

        except Exception as e:
            log(f"      ❌ Extraction error on {url}: {e}")
            return None
        finally:
            try:
                detail_page.close()
            except Exception:
                pass
