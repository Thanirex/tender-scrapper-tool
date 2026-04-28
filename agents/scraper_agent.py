import json
import re
import os
import sys
from pathlib import Path
from playwright.sync_api import sync_playwright

# Resolve paths module from app root regardless of cwd
sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR

class ScraperAgent:
    def __init__(self, config_path="sites_config.json"):
        with open(config_path, 'r') as f:
            self.config = json.load(f)

    def _do_search(self, page, site, keyword):
        """Navigate to site and perform a keyword search. Returns count of results."""
        page.goto(site['url'])
        page.wait_for_load_state("networkidle")
        page.fill(site['search_input_selector'], keyword)
        page.click(site['search_button_selector'])
        page.wait_for_load_state("networkidle")
        return page.locator(site['results_link_selector']).count()

    def search(self, site_key, keyword, log_callback=None, on_result_ready=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        site = self.config.get(site_key)
        if not site:
            log(f"❌ Error: {site_key} not found in config.")
            return []

        results = []
        log(f"🔍 [Scraper] Searching {site_key} for '{keyword}'...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()

            try:
                total = self._do_search(page, site, keyword)
                log(f"   ↳ Found {total} results.")

                for i in range(total):
                    try:
                        self._do_search(page, site, keyword)

                        links = page.locator(site['results_link_selector']).all()
                        if i >= len(links):
                            break

                        links[i].click()
                        page.wait_for_load_state("networkidle")
                        current_url = page.url

                        try:
                            title = page.locator(site['tender_title_selector']).first.text_content().strip()
                        except:
                            try:
                                title = page.locator("h1, h2").first.text_content().strip()
                            except:
                                title = f"Result_{i+1}"

                        try:
                            content = page.locator(site['tender_description_selector']).first.text_content().strip()
                        except:
                            content = "Could not extract content."

                        # Per-tender folder: downloads/{site}/{keyword}/{title}/
                        safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
                        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:35].strip("_. ")
                        tender_dir = DOWNLOADS_DIR / site_key / safe_kw / safe_title
                        tender_dir.mkdir(parents=True, exist_ok=True)

                        txt_path = tender_dir / "page_content.txt"
                        with open(txt_path, "w", encoding="utf-8") as f:
                            f.write(f"Source: {current_url}\nKeyword: {keyword}\nSite: {site_key}\n\n{content}")
                        log(f"   💾 Saved: {txt_path}")

                        rec = {
                            "title": title,
                            "url": current_url,
                            "content": content,
                            "tender_dir": str(tender_dir),
                        }
                        results.append(rec)

                        if on_result_ready:
                            on_result_ready(rec)   # summarise + save Excel immediately

                    except Exception as row_err:
                        log(f"   ⚠️ Error on row {i}: {row_err}")
                        continue

            except Exception as e:
                log(f"❌ Scraping error on {site_key}: {e}")
            finally:
                browser.close()

        return results
