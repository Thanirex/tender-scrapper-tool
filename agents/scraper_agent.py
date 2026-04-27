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

    def search(self, site_key, keyword, log_callback=None):
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
                # First search to get total count
                total = self._do_search(page, site, keyword)
                log(f"   ↳ Found {total} results.")

                for i in range(total):
                    try:
                        # Re-search before every click to get a fresh results page
                        # This is required because ASP.NET go_back() destroys the search state
                        self._do_search(page, site, keyword)

                        links = page.locator(site['results_link_selector']).all()
                        if i >= len(links):
                            break

                        # Click result i on the fresh results page
                        links[i].click()
                        page.wait_for_load_state("networkidle")
                        current_url = page.url

                        # Extract title using site-specific selector
                        try:
                            title = page.locator(site['tender_title_selector']).first.text_content().strip()
                        except:
                            try:
                                title = page.locator("h1, h2").first.text_content().strip()
                            except:
                                title = f"Result_{i+1}"

                        # Extract description
                        try:
                            content = page.locator(site['tender_description_selector']).first.text_content().strip()
                        except:
                            content = "Could not extract content."

                        # Save to a unique .txt file
                        safe_title = re.sub(r'[\\/*?:"<>|]', '', title)[:50]
                        safe_keyword = re.sub(r'[\\/*?:"<>|]', '', keyword)[:20]
                        site_dl_dir = DOWNLOADS_DIR / site_key
                        site_dl_dir.mkdir(parents=True, exist_ok=True)
                        txt_filename = str(site_dl_dir / f"{safe_keyword}_{i+1}_{safe_title}.txt")
                        with open(txt_filename, "w", encoding="utf-8") as f:
                            f.write(f"Source: {current_url}\n")
                            f.write(f"Keyword: {keyword}\n")
                            f.write(f"Site: {site_key}\n\n")
                            f.write(content)
                        log(f"   💾 Saved: {txt_filename}")

                        results.append({
                            "title": title,
                            "url": current_url,
                            "content": content
                        })

                    except Exception as row_err:
                        log(f"   ⚠️ Error on row {i}: {row_err}")
                        continue

            except Exception as e:
                log(f"❌ Scraping error on {site_key}: {e}")
            finally:
                browser.close()

        return results
