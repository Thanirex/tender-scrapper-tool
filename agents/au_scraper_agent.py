import re
import sys
import requests
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from date_utils import is_within_cutoff_ist, extract_date_from_text, get_max_age_hours, is_date_or_deadline_valid
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches, find_negative_keyword


class AUScraperAgent:
    BASE_URL = "https://au.int"
    BIDS_URL = "https://au.int/en/bids"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None, team_id="cnk", max_age_hours=None):
        if max_age_hours is None:
            max_age_hours = get_max_age_hours(team_id)

        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [African Union] Scanning for '{keyword}'...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            try:
                page.goto(self.BIDS_URL, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PlaywrightTimeout:
                    pass

                bids = self._collect_all_bids(page, log)
                log(f"   ↳ {len(bids)} bid(s) found on page")

                n_title_miss = n_neg = n_stale = n_no_date = n_dup = n_opened = 0
                for bid in bids:
                    title        = bid["title"]
                    url          = bid["url"]
                    release_date = bid["release_date"]

                    if not keyword_matches(keyword, title):
                        n_title_miss += 1
                        continue

                    neg = find_negative_keyword(title, team_id=team_id)
                    if neg:
                        n_neg += 1
                        log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}' in title")
                        continue

                    if release_date:
                        if not is_date_or_deadline_valid(release_date, max_age_hours):
                            n_stale += 1
                            log(f"   📅 Skipping '{title[:55]}' — date/deadline {release_date} expired")
                            continue
                        log(f"   ✅ Date / Active Deadline OK: {release_date}")
                    else:
                        n_no_date += 1
                        log(f"   ⚠️ No date in URL for '{title[:55]}' — skipping")
                        continue

                    if db and db.is_duplicate(title, url, team_id=team_id):
                        n_dup += 1
                        log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                        continue

                    n_opened += 1
                    log(f"   📄 Opening: {title[:70]}")
                    base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "au"
                    rec = self._extract_tender(ctx, url, keyword, title, release_date, base, log, db, team_id=team_id)
                    if rec:
                        results.append(rec)
                        if on_result_ready:
                            on_result_ready(rec)

                log(
                    f"   📊 '{keyword}' summary on AU: {len(bids)} bid(s) listed → "
                    f"{n_title_miss} without the keyword in the title, "
                    f"{n_neg} blocked by negative keywords, {n_stale} older than 24h, "
                    f"{n_no_date} missing a date, {n_dup} already collected, "
                    f"{n_opened} opened for full check, {len(results)} saved"
                )

            except Exception as e:
                log(f"❌ AU scrape error: {e}")
            finally:
                browser.close()

        return results

    def _collect_all_bids(self, page, log) -> list:
        """Collect bids across all pages (handles pagination if present)."""
        bids = []
        seen_urls: set = set()

        while True:
            page_bids = self._collect_page_bids(page, log)
            new_bids  = [b for b in page_bids if b["url"] not in seen_urls]
            if not new_bids:
                break   # empty page, or pagination looped back on itself
            seen_urls.update(b["url"] for b in new_bids)
            bids.extend(new_bids)

            # Follow "next page" link if present
            try:
                next_link = page.locator(
                    "a[title='Go to next page'], li.pager__item--next a, a.pager__item--next"
                ).first
                if next_link.is_visible():
                    href = next_link.get_attribute("href") or ""
                    if href:
                        next_url = (self.BASE_URL + href) if href.startswith("/") else href
                        page.goto(next_url, wait_until="domcontentloaded", timeout=30000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=10000)
                        except PlaywrightTimeout:
                            pass
                        continue
            except Exception:
                pass
            break

        return bids

    def _collect_page_bids(self, page, log) -> list:
        """Extract bid rows from the currently loaded table page."""
        try:
            rows = page.eval_on_selector_all(
                "table tr td a",
                """els => els
                    .filter(a => /\\/en\\/bids\\/\\d{8}\\//.test(a.getAttribute('href') || ''))
                    .map(a => ({title: a.textContent.trim(), href: a.href}))"""
            )
        except Exception as e:
            log(f"   ⚠️ Error reading bid table: {e}")
            return []

        bids = []
        for row in rows:
            href  = row.get("href", "")
            title = row.get("title", "").strip()
            if not href or not title:
                continue

            m = re.search(r"/en/bids/(\d{8})/", href)
            if not m:
                continue

            try:
                release_date = datetime.strptime(m.group(1), "%Y%m%d").strftime("%Y-%m-%d")
            except ValueError:
                release_date = ""

            bids.append({"title": title, "url": href, "release_date": release_date})

        return bids

    def _extract_tender(self, ctx, url: str, keyword: str, list_title: str,
                        release_date: str, base_dir: Path, log, db=None, team_id: str = "cnk") -> dict | None:
        detail_page = ctx.new_page()
        try:
            detail_page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                detail_page.locator("h1").wait_for(state="visible", timeout=10000)
            except PlaywrightTimeout:
                log(f"      ⚠️ Page did not render (h1 not visible) — skipping")
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

            neg = find_negative_keyword(title, body_text, team_id=team_id)
            if neg:
                log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on page")
                if db:
                    db.mark_downloaded(title, url, "au", keyword, release_date, team_id=team_id)
                return None

            # Extract bid number and deadline from <p> tags
            bid_number = ""
            deadline   = ""
            try:
                for p_text in detail_page.locator("p").all_inner_texts():
                    p_clean = p_text.strip()
                    if re.search(r"bid\s*number", p_clean, re.IGNORECASE):
                        bid_number = re.sub(r"(?i)bid\s*number\s*:?\s*", "", p_clean).strip()
                    elif " to " in p_clean:
                        m = re.search(
                            r"(\w+\s+\d{1,2},\s+\d{4})\s+to\s+(\w+\s+\d{1,2},\s+\d{4})",
                            p_clean,
                        )
                        if m:
                            deadline = m.group(2).strip()
            except Exception:
                pass

            # Collect all document download links
            try:
                doc_hrefs = detail_page.eval_on_selector_all(
                    "a[href*='/sites/default/files/bids/']",
                    "els => [...new Set(els.map(e => e.href).filter(h => h))]"
                )
            except Exception:
                doc_hrefs = []

            log(f"      📋 {title[:70]}")
            log(f"      📎 {len(doc_hrefs)} document(s) found")

            safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
            safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
            tender_dir = base_dir / safe_kw / safe_title
            tender_dir.mkdir(parents=True, exist_ok=True)

            downloaded = []
            for doc_url in doc_hrefs:
                try:
                    fname      = doc_url.rstrip("/").split("/")[-1].split("?")[0]
                    safe_stem  = re.sub(r'[\\/*?:"<>|]', "_", Path(fname).stem)[:55]
                    safe_ext   = Path(fname).suffix[:10]
                    out_path   = tender_dir / (safe_stem + safe_ext)

                    resp = requests.get(doc_url, timeout=30, stream=True)
                    resp.raise_for_status()
                    with open(out_path, "wb") as f:
                        for chunk in resp.iter_content(8192):
                            f.write(chunk)
                    downloaded.append(str(out_path))
                    log(f"      💾 {safe_stem + safe_ext}")
                except Exception as dl_err:
                    log(f"      ⚠️ Download failed ({doc_url}): {dl_err}")

            if body_text:
                txt_path = tender_dir / "page_content.txt"
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"Source: {url}\n\n{body_text}")
                if not downloaded:
                    downloaded.append(str(txt_path))
                    log(f"      📝 No documents — saved page text")
                else:
                    log(f"      📝 Page text saved alongside documents")

            if db:
                db.mark_downloaded(title, url, "au", keyword, release_date, team_id=team_id)

            return {
                "keyword":      keyword,
                "title":        title,
                "url":          url,
                "page_text":    body_text,
                "files":        downloaded,
                "tender_dir":   str(tender_dir),
                "release_date": release_date,
                "deadline":     deadline,
                "bid_number":   bid_number,
                "site":         "au",
            }

        except Exception as e:
            log(f"      ❌ Extraction error on {url}: {e}")
            return None
        finally:
            try:
                detail_page.close()
            except Exception:
                pass
