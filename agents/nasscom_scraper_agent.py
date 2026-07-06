import re
import os
import sys
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches


class NasscomScraperAgent:
    PAGE_URL = "https://www.nasscomfoundation.org/requestproposal"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [Nasscom] Scanning for '{keyword}'...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(self.PAGE_URL, wait_until="domcontentloaded", timeout=40000)
                page.wait_for_timeout(3000)

                items = page.locator("#PressReleases li").all()
                log(f"   ↳ {len(items)} RFP entries on page.")

                for item in items:
                    try:
                        # Full text of <li>, strip out "Click here" link text to get clean title
                        raw = item.text_content().strip()
                        title = re.sub(r'\s*\|\s*click\s+here', '', raw, flags=re.IGNORECASE)
                        title = re.sub(r'\s*click\s+here', '', title, flags=re.IGNORECASE)
                        title = title.rstrip(' -|').strip()

                        if not title:
                            continue

                        if not keyword_matches(keyword, title):
                            continue

                        # All PDF links in this <li> — take the last one (main doc, not cover letter)
                        pdf_els   = item.locator('a[href*=".pdf"]').all()
                        pdf_links = [el.evaluate('e => e.href') for el in pdf_els]
                        pdf_links = [u for u in pdf_links if u]
                        if not pdf_links:
                            log(f"   ⚠️ No PDF found for: {title[:55]}")
                            continue

                        pdf_url = pdf_links[-1]

                        # Dedup by PDF URL — title alone could collide across keywords
                        if db and db.is_duplicate(title, pdf_url):
                            log(f"   ⏩ Duplicate skipped: {title[:60]}")
                            continue

                        safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
                        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:35].strip("_. ")
                        base       = Path(output_dir) if output_dir else DOWNLOADS_DIR / "nasscom"
                        tender_dir = base / safe_kw / safe_title
                        tender_dir.mkdir(parents=True, exist_ok=True)

                        pdf_filename = os.path.basename(pdf_url.split("?")[0]) or "document.pdf"
                        pdf_path     = tender_dir / pdf_filename

                        log(f"   ⬇️ Downloading: {pdf_filename}")
                        try:
                            resp = requests.get(pdf_url, timeout=30)
                            resp.raise_for_status()
                            with open(pdf_path, "wb") as f:
                                f.write(resp.content)
                        except Exception as dl_err:
                            log(f"   ❌ Download failed: {dl_err}")
                            continue

                        if db:
                            db.mark_downloaded(title, pdf_url, "nasscom", keyword, "")

                        rec = {
                            "keyword":    keyword,
                            "title":      title,
                            "url":        pdf_url,
                            "files":      [str(pdf_path)],
                            "tender_dir": str(tender_dir),
                            "site":       "nasscom",
                        }
                        results.append(rec)

                        if on_result_ready:
                            on_result_ready(rec)

                    except Exception as row_err:
                        log(f"   ⚠️ Row error: {row_err}")
                        continue

            except Exception as e:
                log(f"❌ Nasscom scrape error: {e}")
            finally:
                browser.close()

        return results
