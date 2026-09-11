import json
import re
import os
import sys
import requests
from pathlib import Path
from urllib.parse import quote
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Resolve paths / utils from app root regardless of cwd
sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from date_utils import is_within_cutoff_ist, extract_date_from_text, get_max_age_hours, is_date_or_deadline_valid
from keyword_utils import keyword_matches, find_negative_keyword

_STEALTH_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_CHALLENGE_TITLES = ("just a moment", "attention required")
_SCRIPT_RE = re.compile(r"<script\b[^>]*>.*?</script>", re.S | re.I)

# For each result link, the date inside that result's own row: climb from the
# link until an ancestor holds exactly one date element. An ancestor holding
# several has grown past this row into the whole list, so stop with no date.
_JS_RESULT_ROWS = """
(els, dateSel) => els.map(e => {
    let date = null;
    if (dateSel) {
        for (let node = e.parentElement; node && node !== document.body; node = node.parentElement) {
            const found = node.querySelectorAll(dateSel);
            if (found.length === 1) { date = found[0].textContent.trim(); break; }
            if (found.length > 1) break;
        }
    }
    return { href: e.href, date };
})
"""


class ScraperAgent:
    def __init__(self, config_path="sites_config.json"):
        with open(config_path, 'r') as f:
            self.config = json.load(f)

    @staticmethod
    def _settle(page, timeout=30000):
        """Wait for network quiet, but never fail on it — ad/analytics beacons
        keep some sites (the jobin* family) from ever going idle."""
        try:
            page.wait_for_load_state("networkidle", timeout=timeout)
        except PlaywrightTimeout:
            pass

    def _do_search(self, page, site, keyword):
        """Navigate to site and perform a keyword search. Returns count of results.

        Navigation waits for the DOM only: the default "load" also waits for
        every image, ad and tracker, which pushed slow sites past the timeout
        even though the results were already on the page.
        """
        template = site.get("search_url_template")
        if template:
            # Sites that encode the keyword directly in the URL (no form submit needed)
            url = template.replace("{keyword}", quote(keyword))
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(3000)
        else:
            page.goto(site['url'], wait_until="domcontentloaded", timeout=45000)
            self._settle(page)
            page.fill(site['search_input_selector'], keyword)
            page.click(site['search_button_selector'])
            self._settle(page)
        return page.locator(site['results_link_selector']).count()

    @staticmethod
    def _is_challenge(title: str) -> bool:
        title = (title or "").lower()
        return any(t in title for t in _CHALLENGE_TITLES)

    def _goto_result(self, page, url: str, log) -> bool:
        """Open a result page directly, getting past bot-protection interstitials.

        Cloudflare challenges the headless browser itself ('Just a moment...')
        on AfDB detail pages and the challenge never clears there, while the
        same URL fetched over plain HTTP is served normally. So after one short
        wait the page is fetched with requests and loaded into the tab, and the
        usual selectors run against that copy.
        """
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        if not self._is_challenge(page.title()):
            return True
        page.wait_for_timeout(5000)
        if not self._is_challenge(page.title()):
            return True

        try:
            resp = requests.get(
                url, timeout=45,
                headers={"User-Agent": _STEALTH_UA, "Accept-Language": "en-US,en;q=0.9"},
            )
            m = re.search(r"<title[^>]*>(.*?)</title>", resp.text, re.S | re.I)
            if resp.ok and not self._is_challenge(m.group(1) if m else ""):
                page.set_content(_SCRIPT_RE.sub("", resp.text),
                                 wait_until="domcontentloaded", timeout=30000)
                log(f"   ↪️ Browser was challenged — loaded the page over plain HTTP instead")
                return True
        except Exception:
            pass

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
                    user_agent=_STEALTH_UA,
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

                # URL-template sites (e.g. AfDB, jobin*) expose stable result
                # hrefs — collect them once and open each directly. Re-running
                # the search for every row triggers Cloudflare's "Just a moment"
                # challenge on repeat visits, which used to return 0 links and
                # silently abort the loop before processing a single result.
                rows = []
                if site.get("search_url_template"):
                    try:
                        raw_rows = page.eval_on_selector_all(
                            site['results_link_selector'], _JS_RESULT_ROWS,
                            site.get("listing_date_selector"),
                        )
                    except Exception:
                        raw_rows = []
                    # A result often carries several links to the same page
                    # (logo + title), which opened — and counted — it twice.
                    seen = set()
                    for r in raw_rows:
                        if r["href"] and r["href"] not in seen:
                            seen.add(r["href"])
                            rows.append(r)
                    if rows:
                        total = len(rows)
                hrefs = [r["href"] for r in rows]

                log(f"   ↳ Found {total} results.")

                # Tallies for the closing summary — every result is accounted for
                n_title_miss = n_neg = n_stale = n_no_date = n_dup = n_err = 0

                for i in range(total):
                    try:
                        if hrefs:
                            if i >= len(hrefs):
                                break
                            # Listing already shows the date (listing_date_selector):
                            # drop stale results without opening them. On AfDB
                            # most matches are years old, and each detail page
                            # visit is a bot-protection gamble.
                            listed = rows[i].get("date")
                            if (listed and not site.get("skip_date_filter")
                                    and not is_date_or_deadline_valid(listed, max_age_hours)):
                                n_stale += 1
                                log(f"   📅 Skipping result {i+1} — listed {listed}, "
                                    f"outside the publication window")
                                continue
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
                            self._settle(page)

                        # A page loaded over plain HTTP (see _goto_result) sits
                        # at about:blank, so fall back to the result's own href.
                        current_url = page.url
                        if hrefs and not current_url.startswith("http"):
                            current_url = hrefs[i]

                        try:
                            title = page.locator(site['tender_title_selector']).first.text_content().strip()
                        except Exception:
                            try:
                                title = page.locator("h1, h2").first.text_content().strip()
                            except Exception:
                                title = f"Result_{i+1}"

                        # DevNet uses ASP.NET postbacks — page.url never changes after click.
                        # Try multiple strategies to get a stable, shareable job URL.
                        if "devnetjobs" in current_url.lower() and "job_id=" not in current_url:
                            job_url = None

                            # Strategy 1: any anchor on the detail page whose href has job_id=
                            if not job_url:
                                try:
                                    # Local name — must not shadow the outer `hrefs`
                                    # list of result links driving this loop.
                                    job_hrefs = page.eval_on_selector_all(
                                        "a[href*='job_id']",
                                        "els => els.map(e => e.href)"
                                        ".filter(h => h.includes('job_id='))"
                                    )
                                    if job_hrefs:
                                        job_url = job_hrefs[0]
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

                        # ── Keyword relevance check ──────────────────────────────
                        if not keyword_matches(keyword, title):
                            n_title_miss += 1
                            log(f"   🚫 Skipping '{title[:55]}' — keyword '{keyword}' not in title")
                            continue

                        neg = find_negative_keyword(title, team_id=team_id)
                        if neg:
                            n_neg += 1
                            log(f"   🚫 Skipping '{title[:55]}' — negative keyword '{neg}' in title")
                            continue

                        try:
                            content = page.locator(site['tender_description_selector']).first.text_content().strip()
                        except Exception:
                            content = "Could not extract content."

                        # ── Negative keyword check on the description ────────────
                        neg = find_negative_keyword(title, content, team_id=team_id)
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
                            db.mark_downloaded(title, current_url, site_key, keyword, pub_date, team_id=team_id)

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
                    f"{n_neg} blocked by negative keywords, {n_stale} older than {max_age_hours}h, "
                    f"{n_no_date} missing a publish date, {n_dup} already collected, "
                    f"{n_err} errored, {len(results)} saved"
                )

            except Exception as e:
                log(f"❌ Scraping error on {site_key}: {e}")
            finally:
                browser.close()

        return results
