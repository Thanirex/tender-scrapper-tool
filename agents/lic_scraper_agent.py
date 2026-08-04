import re
import sys
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches, find_negative_keyword
from date_utils import is_within_cutoff_ist, extract_date_from_text, get_max_age_hours, is_date_or_deadline_valid

_JS_COLLECT_PAGE_ROWS = """
() => {
    const rows = Array.from(document.querySelectorAll('table.tender-table tbody tr, .dataTables_wrapper table tbody tr'));
    const results = [];

    for (const tr of rows) {
        const descTd = tr.querySelector('td[data-label="Tender Description"]') || tr.querySelectorAll('td')[2];
        if (!descTd) continue;

        const a = descTd.querySelector('a');
        if (!a) continue;

        const title = (a.textContent || '').replace(/\\s+/g, ' ').trim();
        const detailUrl = a.href || '';
        if (!title || title.length < 3) continue;

        const regionTd = tr.querySelector('td[data-label="Region"]') || tr.querySelectorAll('td')[0];
        const deptTd = tr.querySelector('td[data-label="Department"]') || tr.querySelectorAll('td')[1];
        const startDateTd = tr.querySelector('td[data-label="Start Date"]') || tr.querySelectorAll('td')[3];
        const endDateTd = tr.querySelector('td[data-label="End Date"]') || tr.querySelectorAll('td')[4];

        const region = regionTd ? regionTd.textContent.trim() : '';
        const dept = deptTd ? deptTd.textContent.trim() : '';
        const startDate = startDateTd ? startDateTd.textContent.trim() : '';
        const endDate = endDateTd ? endDateTd.textContent.trim() : '';

        const fullRowText = `${title} Region: ${region} Dept: ${dept} Publication Date: ${startDate} Submission Deadline: ${endDate}`;

        results.push({
            title,
            detailUrl,
            region,
            dept,
            startDate,
            endDate,
            deadline: endDate,
            fullRowText
        });
    }

    return results;
}
"""


class LICScraperAgent:
    PAGE_URL = "https://licindia.in/tenders"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None, team_id="tmi", max_age_hours=None):
        if max_age_hours is None:
            max_age_hours = get_max_age_hours(team_id)
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                try:
                    print(msg)
                except UnicodeEncodeError:
                    print(msg.encode("ascii", "replace").decode("ascii"))

        results = []
        log(f"🔍 [LIC] Scanning for '{keyword}'...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            try:
                page.goto(self.PAGE_URL, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass
                page.wait_for_timeout(2000)

                # Step 1: Set entries per page dropdown to 100 if present
                try:
                    length_select = page.query_selector("select[name*='length'], .dataTables_length select, select")
                    if length_select:
                        options = length_select.query_selector_all("option")
                        val_100 = None
                        for opt in options:
                            if (opt.text_content() or "").strip() == "100" or opt.get_attribute("value") == "100":
                                val_100 = opt.get_attribute("value") or "100"
                                break
                        if val_100:
                            length_select.select_option(val_100)
                            log("   ⚙️ Selected 100 entries per page")
                            page.wait_for_timeout(1500)
                except Exception as e:
                    log(f"   ⚠️ Entries dropdown select note: {e}")

                # Step 2: Fill keyword into search input box
                try:
                    search_box = page.query_selector("input[type='search'], .dataTables_filter input, input[aria-controls]")
                    if search_box:
                        search_box.fill("")
                        search_box.fill(keyword)
                        log(f"   🔍 Searched for keyword '{keyword}' in portal search box")
                        page.wait_for_timeout(2000)
                except Exception as e:
                    log(f"   ⚠️ Search box note: {e}")

                # Step 3: Collect all tender rows across search result pages via pagination loop
                all_tenders = []
                seen_titles = set()
                page_num = 1

                while True:
                    rows = page.evaluate(_JS_COLLECT_PAGE_ROWS) or []
                    log(f"   ↳ Page {page_num}: found {len(rows)} tender row(s)")

                    added_on_page = 0
                    for r in rows:
                        title_key = (r["title"], r["detailUrl"])
                        if title_key not in seen_titles:
                            seen_titles.add(title_key)
                            all_tenders.append(r)
                            added_on_page += 1

                    # Check for Next page button
                    next_btn = page.query_selector(
                        ".paginate_button.next:not(.disabled), #tenderTable_next:not(.disabled), "
                        "a.next:not(.disabled), a:has-text('Next'):not(.disabled)"
                    )
                    if next_btn and added_on_page > 0:
                        try:
                            next_btn.click()
                            page_num += 1
                            page.wait_for_timeout(2000)
                        except Exception:
                            break
                    else:
                        break

                log(f"   ↳ Total extracted tenders across all pages: {len(all_tenders)}")

                base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "lic"

                n_miss = n_neg = n_stale = n_dup = 0
                for tender in all_tenders:
                    title       = tender["title"]
                    detail_url  = tender["detailUrl"] or self.PAGE_URL
                    row_text    = tender["fullRowText"]
                    start_date  = tender.get("startDate", "").strip()
                    end_date    = tender.get("endDate", "").strip()
                    deadline    = tender.get("deadline", "") or end_date

                    if not keyword_matches(keyword, title, row_text):
                        n_miss += 1
                        continue

                    neg = find_negative_keyword(title, row_text)
                    if neg:
                        n_neg += 1
                        log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}'")
                        continue

                    # ── Date / Publication Cutoff Check ────────────────────────
                    pub_date = start_date
                    if pub_date:
                        if not is_within_cutoff_ist(pub_date, max_age_hours):
                            n_stale += 1
                            log(f"   📅 Skipping '{title[:60]}' — Start Date {pub_date} older than {max_age_hours}h cutoff")
                            continue
                        log(f"   ✅ Publication Date OK: {pub_date}")
                    else:
                        if deadline and not is_date_or_deadline_valid(deadline, max_age_hours):
                            n_stale += 1
                            log(f"   📅 Skipping '{title[:60]}' — Deadline {deadline} expired/invalid")
                            continue

                    # Website webpage URL to show for viewing (NOT raw PDF)
                    webpage_url = detail_url if detail_url else self.PAGE_URL

                    if db and db.is_duplicate(title, webpage_url):
                        n_dup += 1
                        log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                        continue

                    log(f"   📄 {title[:70]}")

                    safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
                    safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
                    tender_dir = base / safe_kw / safe_title
                    tender_dir.mkdir(parents=True, exist_ok=True)

                    # Fetch detail page HTML and download PDF attachments
                    detail_text, doc_links = self._fetch_detail_page(detail_url, log)

                    # If pub_date wasn't in table, try to extract from detail page text
                    if not pub_date and detail_text:
                        extracted = extract_date_from_text(detail_text)
                        if extracted and not is_within_cutoff_ist(extracted, max_age_hours):
                            n_stale += 1
                            log(f"   📅 Skipping '{title[:60]}' — extracted date {extracted} older than {max_age_hours}h")
                            continue
                        pub_date = extracted or pub_date

                    combined_text = f"Publication Date: {pub_date}\nSubmission Deadline: {deadline}\n{row_text}\n\n=== TENDER DETAILS ===\n{detail_text}".strip()

                    downloaded = []
                    for doc_url in doc_links:
                        saved = self._download_file(doc_url, tender_dir, log)
                        if saved and saved not in downloaded:
                            downloaded.append(saved)

                    # Always save page text for summarizer
                    txt_path = tender_dir / "page_content.txt"
                    with open(txt_path, "w", encoding="utf-8") as f:
                        f.write(f"Source: {webpage_url}\n\n{combined_text}")

                    if not downloaded:
                        downloaded.append(str(txt_path))
                        log(f"      📝 No PDF documents — saved entry text only")
                    else:
                        log(f"      📝 Entry text also saved")

                    if db:
                        db.mark_downloaded(title, webpage_url, "lic", keyword, pub_date or deadline)

                    rec = {
                        "keyword":        keyword,
                        "title":          title,
                        "url":            webpage_url,
                        "page_text":      combined_text,
                        "files":          downloaded,
                        "tender_dir":     str(tender_dir),
                        "site":           "lic",
                        "published_date": pub_date or deadline,
                        "startDate":      pub_date,
                        "endDate":        end_date,
                        "deadline":       deadline,
                    }
                    results.append(rec)
                    if on_result_ready:
                        on_result_ready(rec)

                log(
                    f"   📊 '{keyword}' summary on LIC: {len(all_tenders)} entry(ies) listed → "
                    f"{n_miss} without keyword, {n_neg} blocked by negative keywords, "
                    f"{n_stale} older than {max_age_hours}h cutoff, "
                    f"{n_dup} already collected, {len(results)} saved"
                )

            except Exception as e:
                log(f"❌ LIC scrape error: {e}")
            finally:
                browser.close()

        return results

    def _fetch_detail_page(self, url: str, log) -> tuple[str, list[str]]:
        if not url or url == self.PAGE_URL:
            return "", []

        try:
            resp = requests.get(
                url,
                timeout=30,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                }
            )
            resp.raise_for_status()

            html = resp.text
            # Clean HTML to readable text snippet
            clean_text = re.sub(r'<script.*?>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r'<style.*?>.*?</style>', '', clean_text, flags=re.DOTALL | re.IGNORECASE)
            clean_text = re.sub(r'<[^>]+>', ' ', clean_text)
            clean_text = re.sub(r'\s+', ' ', clean_text).strip()

            # Extract PDF and document download links
            raw_links = re.findall(r'href=["\']([^"\']+)["\']', html, flags=re.IGNORECASE)
            doc_links = []

            IGNORE_PATTERNS = [
                'certificate-of-registration', 'anti-bribery', 'anti-fraud',
                'download-forms', 'privacy-policy', 'terms-conditions',
                'citizens-charter', 'equal-opportunity'
            ]

            for link in raw_links:
                link_lower = link.lower()
                if any(p in link_lower for p in IGNORE_PATTERNS):
                    continue

                if re.search(r'\.(png|jpe?g|gif|svg|css|js)(\b|/|\?|$)', link, re.IGNORECASE):
                    continue

                if re.search(r'\.(pdf|docx?|xlsx?|zip)(\b|/|\?|$)', link, re.IGNORECASE) or '/documents/' in link:
                    full_link = requests.compat.urljoin(url, link)
                    if full_link not in doc_links:
                        doc_links.append(full_link)

            return clean_text[:10_000], doc_links
        except Exception as e:
            log(f"      ⚠️ Fetching detail page error ({url[-50:]}): {e}")
            return "", []

    def _download_file(self, url: str, tender_dir: Path, log) -> "str | None":
        try:
            resp = requests.get(
                url,
                timeout=45,
                stream=True,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    )
                }
            )
            resp.raise_for_status()

            if "text/html" in resp.headers.get("content-type", ""):
                log(f"      ⚠️ HTML response for {url[-40:]} — skipping")
                return None

            extracted_filename = None

            # 1. Check Content-Disposition header
            cd = resp.headers.get("content-disposition", "")
            if cd:
                m_cd = re.search(r'filename=["\']?([^"\';\r\n]+)["\']?', cd, re.IGNORECASE)
                if m_cd:
                    extracted_filename = m_cd.group(1).strip()

            # 2. Extract from URL path segments containing extensions
            if not extracted_filename:
                import urllib.parse
                parts = [p for p in url.split("/") if p]
                for part in parts:
                    part_unquoted = urllib.parse.unquote(part).strip()
                    if re.search(r'\.(pdf|docx?|xlsx?|zip)$', part_unquoted, re.IGNORECASE):
                        extracted_filename = part_unquoted
                        break

            # 3. Fallback
            if not extracted_filename:
                extracted_filename = "document.pdf"

            fname = extracted_filename
            safe_stem = re.sub(r'[\\/*?:"<>|]', "_", Path(fname).stem)[:60].strip("_. ")
            safe_ext  = Path(fname).suffix[:10] or ".pdf"
            if not safe_ext.startswith("."):
                safe_ext = "." + safe_ext

            out_path = tender_dir / (safe_stem + safe_ext)
            counter = 1
            while out_path.exists():
                out_path = tender_dir / f"{safe_stem}_{counter}{safe_ext}"
                counter += 1

            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            log(f"      💾 {out_path.name}")
            return str(out_path)
        except Exception as e:
            log(f"      ⚠️ Download failed ({url[-60:]}): {e}")
            return None
