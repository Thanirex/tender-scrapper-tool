import json
import re
import os
import sys
from pathlib import Path
from urllib.parse import quote
from playwright.sync_api import sync_playwright

# Resolve paths / utils from app root regardless of cwd
sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from date_utils import is_within_cutoff_ist, extract_date_from_text, get_max_age_hours, is_date_or_deadline_valid
from keyword_utils import keyword_matches, find_negative_keyword


class ScraperAgent:
    def __init__(self, config_path="sites_config.json"):
        with open(config_path, 'r') as f:
            self.config = json.load(f)

    def _do_search(self, page, site, keyword):
        """Navigate to site and perform a keyword search. Returns count of results."""
        template = site.get("search_url_template")
        if template:
            # Sites that encode the keyword directly in the URL (no form submit needed)
            url = template.replace("{keyword}", quote(keyword))
            page.goto(url)
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)
        else:
            page.goto(site['url'])
            page.wait_for_load_state("networkidle")
            page.fill(site['search_input_selector'], keyword)
            page.click(site['search_button_selector'])
            page.wait_for_load_state("networkidle")
        return page.locator(site['results_link_selector']).count()

    def _goto_result(self, page, url: str, log) -> bool:
        """Open a result page directly, waiting out bot-protection interstitials
        (Cloudflare shows 'Just a moment...' on rapid repeat visits)."""
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        for _ in range(3):
            title = (page.title() or "").lower()
            if "just a moment" not in title and "attention required" not in title:
                return True
            page.wait_for_timeout(5000)
        log(f"   ⚠️ Bot-protection page blocked access to: {url[:80]}")
        return False

    def _extract_pub_date(self, page, site) -> str | None:
        """
        Try to find the publication date for the currently loaded tender page.
        Attempts a site-specific CSS selector first (if configured), then falls
        back to regex scanning the full body text.
        Returns the raw date string or None.
        """
        selector = site.get("date_selector")
        if selector:
            try:
                return page.locator(selector).first.text_content().strip()
            except Exception:
                pass
        try:
            body_text = page.locator("body").text_content()
        except Exception:
            return None
        return extract_date_from_text(body_text)

    def search(self, site_key, keyword, log_callback=None, on_result_ready=None, db=None, team_id="cnk", max_age_hours=None):
        if max_age_hours is None:
            max_age_hours = get_max_age_hours(team_id)

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
            if site.get("requires_stealth"):
                browser = p.chromium.launch(
                    headless=True,
                    args=["--disable-blink-features=AutomationControlled"],
                )
                context = browser.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/125.0.0.0 Safari/537.36"
                    ),
                    viewport={"width": 1280, "height": 800},
                    locale="en-US",
                )
                page = context.new_page()
                page.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
                )
            else:
                browser = p.chromium.launch(headless=True)
                context = browser.new_context()
                page = context.new_page()

            try:
                total = self._do_search(page, site, keyword)
                log(f"   ↳ Found {total} results.")

                # Tallies for the closing summary — every result is accounted for
                n_title_miss = n_neg = n_stale = n_no_date = n_dup = n_err = 0

                # URL-template sites (e.g. AfDB) expose stable result hrefs —
                # collect them once and open each directly. Re-running the
                # search for every row triggers Cloudflare's "Just a moment"
                # challenge on repeat visits, which used to return 0 links and
                # silently abort the loop before processing a single result.
                hrefs = []
                if site.get("search_url_template"):
                    try:
                        hrefs = [
                            h for h in page.eval_on_selector_all(
                                site['results_link_selector'],
                                "els => els.map(e => e.href)",
                            ) if h
                        ]
                    except Exception:
                        hrefs = []

                for i in range(total):
                    try:
                        if hrefs:
                            if i >= len(hrefs):
                                break
                            if not self._goto_result(page, hrefs[i], log):
                                continue
                        else:
                            self._do_search(page, site, keyword)

                            links = page.locator(site['results_link_selector']).all()
                            if i >= len(links):
                                log(f"   ⚠️ Re-search returned only {len(links)} link(s), "
                                    f"expected {total} — stopping early")
                                break

                            links[i].click()
                            page.wait_for_load_state("networkidle")

                        current_url = page.url

                        # DevNet uses ASP.NET postbacks — page.url never changes after click.
                        # Try multiple strategies to get a stable, shareable job URL.
                        if "devnetjobs" in current_url.lower() and "job_id=" not in current_url:
                            job_url = None

                            # Strategy 1: any anchor on the detail page whose href has job_id=
                            if not job_url:
                                try:
                                    hrefs = page.eval_on_selector_all(
                                        "a[href*='job_id']",
                                        "els => els.map(e => e.href)"
                                        ".filter(h => h.includes('job_id='))"
                                    )
                                    if hrefs:
                                        job_url = hrefs[0]
                                except Exception:
                                    pass

                            # Strategy 2: form action contains job_id (original fallback)
                            if not job_url:
                                try:
                                    form_action = page.locator("form").first.get_attribute("action")
                                    if form_action and "job_id=" in form_action:
                                        clean = form_action.lstrip("./")
                                        job_url = (
                                            clean if clean.startswith("http")
                                            else f"https://devnetjobsindia.org/{clean}"
                                        )
                                except Exception:
                                    pass

                            # Strategy 3: og:url meta tag
                            if not job_url:
                                try:
                                    og = page.locator("meta[property='og:url']").get_attribute("content")
                                    if og and "job_id=" in og:
                                        job_url = og
                                except Exception:
                                    pass

                            if job_url:
                                current_url = job_url
                            else:
                                log(f"   ⚠️ Could not find a stable job URL for '{title[:55]}' — link may not work")

                        try:
                            title = page.locator(site['tender_title_selector']).first.text_content().strip()
                        except Exception:
                            try:
                                title = page.locator("h1, h2").first.text_content().strip()
                            except Exception:
                                title = f"Result_{i+1}"

                        # ── Keyword relevance check ──────────────────────────────
                        if not keyword_matches(keyword, title):
                            n_title_miss += 1
                            log(f"   🚫 Skipping '{title[:55]}' — keyword '{keyword}' not in title")
                            continue

                        neg = find_negative_keyword(title)
                        if neg:
                            n_neg += 1
                            log(f"   🚫 Skipping '{title[:55]}' — negative keyword '{neg}' in title")
                            continue

                        try:
                            content = page.locator(site['tender_description_selector']).first.text_content().strip()
                        except Exception:
                            content = "Could not extract content."

                        # ── Negative keyword check on the description ────────────
                        neg = find_negative_keyword(title, content)
                        if neg:
                            n_neg += 1
                            log(f"   🚫 Rejected '{title[:55]}' — negative keyword '{neg}' in description")
                            if db:
                                db.mark_downloaded(title, current_url, site_key, keyword, "", team_id=team_id)
                            continue

                        # ── Date filter ──────────────────────────────────────────
                        # Sites that only expose a deadline (not a publish date) set
                        # skip_date_filter=true in sites_config.json.  For those sites
                        # we skip the date check entirely and rely solely on dedup to
                        # prevent re-downloading tenders seen in previous runs.
                        if site.get("skip_date_filter"):
                            pub_date = ""
                        else:
                            pub_date = self._extract_pub_date(page, site)
                            if pub_date:
                                if not is_date_or_deadline_valid(pub_date, max_age_hours):
                                    n_stale += 1
                                    log(f"   📅 Skipping '{title[:55]}' — date {pub_date} expired (outside publication window & past deadline)")
                                    continue
                                log(f"   ✅ Date / Active Deadline OK: {pub_date}")
                            else:
                                n_no_date += 1
                                log(f"   ⚠️ No publication date found for '{title[:55]}' — skipping")
                                continue

                        # ── Deduplication check ──────────────────────────────────
                        if db and db.is_duplicate(title, current_url, team_id=team_id):
                            n_dup += 1
                            log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                            continue

                        # Per-tender folder: downloads/{site}/{keyword}/{title}/
                        safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
                        safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:35].strip("_. ")
                        tender_dir = DOWNLOADS_DIR / site_key / safe_kw / safe_title
                        tender_dir.mkdir(parents=True, exist_ok=True)

                        txt_path = tender_dir / "page_content.txt"
                        with open(txt_path, "w", encoding="utf-8") as f:
                            f.write(f"Source: {current_url}\nKeyword: {keyword}\nSite: {site_key}\n\n{content}")
                        log(f"   💾 Saved: {txt_path}")

                        # Mark in DB so other keywords don't re-download the same tender
                        if db:
                            db.mark_downloaded(title, current_url, site_key, keyword, pub_date)

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
                        n_err += 1
                        log(f"   ⚠️ Error on row {i}: {row_err}")
                        continue

                log(
                    f"   📊 '{keyword}' summary on {site_key}: {total} result(s) → "
                    f"{n_title_miss} without the keyword in the title, "
                    f"{n_neg} blocked by negative keywords, {n_stale} older than 24h, "
                    f"{n_no_date} missing a publish date, {n_dup} already collected, "
                    f"{n_err} errored, {len(results)} saved"
                )

            except Exception as e:
                log(f"❌ Scraping error on {site_key}: {e}")
            finally:
                browser.close()

        return results
