import os
import re
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from date_utils import is_within_cutoff_ist, extract_date_from_text, get_max_age_hours, is_deadline_active
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches, find_negative_keyword

# JS snippet to extract RFP/EOI cards from NGOBOX listing page
_JS_EXTRACT_CARDS = r"""
() => {
    const BASE = "https://ngobox.org";
    const entries = [];
    const seen = new Set();

    // Look for all links pointing to full RFP/EOI detail pages
    const anchors = Array.from(document.querySelectorAll('a[href]'));
    for (const a of anchors) {
        let href = a.href || '';
        if (!href || !href.startsWith('http')) continue;

        // NGOBOX detail page URLs follow pattern full_rfp_eoi_* or full_eoi_* or full_rfp_*
        if (href.includes('full_rfp') || href.includes('full_eoi') || href.includes('/full_rfp_eoi_')) {
            if (seen.has(href)) continue;
            seen.add(href);

            const title = a.textContent.trim();
            if (!title || title.length < 5) continue;
            if (title.toLowerCase().includes('post added') || title.toLowerCase().includes('read more')) continue;

            // Try to extract date context from parent card element
            let cardText = '';
            let p = a.parentElement;
            for (let i = 0; i < 4; i++) {
                if (p) { cardText += ' ' + (p.textContent || ''); p = p.parentElement; }
            }

            // Look for deadline date in card text (e.g. "Deadline: 07 Aug. 2026" or "07 Aug 2026")
            let published = '';
            const m = cardText.match(/Deadline[:\s]*([0-9]{1,2}\s+[A-Za-z]{3,9}\.?,?\s+[0-9]{4})/i) ||
                      cardText.match(/Post Added[:\s]*([0-9]{1,2}\s+[A-Za-z]{3,9}\.?,?\s+[0-9]{4})/i) ||
                      cardText.match(/\b([0-9]{1,2}\s+[A-Za-z]{3,9}\.?,?\s+[0-9]{4})\b/i);
            if (m) {
                published = m[1].trim();
            }

            entries.push({ title, url: href, published });
        }
    }
    return entries;
}
"""


class NGOBOXScraperAgent:
    BASE_URL    = "https://ngobox.org"
    LISTING_URL = "https://ngobox.org/rfp_eoi_listing.php"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None, team_id="tmi", max_age_hours=None):
        if max_age_hours is None:
            max_age_hours = get_max_age_hours(team_id)

        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [NGOBOX] Scanning for '{keyword}'...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            ctx     = browser.new_context(
                accept_downloads=True,
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()
            try:
                log(f"   🌐 Opening listing page: {self.LISTING_URL}")
                page.goto(self.LISTING_URL, wait_until="domcontentloaded", timeout=45000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PlaywrightTimeout:
                    pass

                # Fill search box if search input exists
                search_input = page.locator("input#searchme")
                if search_input.count() > 0:
                    log(f"   ⌨️ Submitting search query: '{keyword}'")
                    search_input.fill(keyword)
                    
                    search_btn = page.locator("i.fa-search, .search-btn, button:has(i.fa-search)")
                    if search_btn.count() > 0:
                        search_btn.first.click()
                    else:
                        page.keyboard.press("Enter")
                        
                    page.wait_for_timeout(3500)
                    try:
                        page.wait_for_load_state("networkidle", timeout=8000)
                    except PlaywrightTimeout:
                        pass
                else:
                    log("   ⚠️ Search input input#searchme not found — reading default listing")

                cards = page.evaluate(_JS_EXTRACT_CARDS)
                log(f"   ↳ Found {len(cards)} card(s) on listing page")

                n_title_miss = n_neg = n_stale = n_dup = n_opened = 0

                for card in cards:
                    title     = card["title"]
                    detail_url = card["url"]
                    published = card.get("published", "")

                    # ── Keyword filter ──────────────────────────────────────
                    if not keyword_matches(keyword, title):
                        n_title_miss += 1
                        continue

                    neg = find_negative_keyword(title, team_id=team_id)
                    if neg:
                        n_neg += 1
                        log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}' in title")
                        continue

                    # ── Deadline active check ────────────────────────────────
                    if published:
                        if not is_deadline_active(published):
                            n_stale += 1
                            log(f"   📅 Skipping '{title[:60]}' — deadline expired ({published})")
                            continue
                        log(f"   ✅ Deadline active: {published}")

                    # ── Dedup check using NGOBOX detail page URL ─────────────
                    if db and db.is_duplicate(title, detail_url, team_id=team_id):
                        n_dup += 1
                        log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                        continue

                    n_opened += 1
                    log(f"   📄 Opening detail notice: {title[:70]}")
                    base_out = Path(output_dir) if output_dir else DOWNLOADS_DIR / "ngobox"
                    rec = self._extract_detail(ctx, detail_url, keyword, title, published, base_out, log, db, max_age_hours=max_age_hours, team_id=team_id)
                    if rec:
                        results.append(rec)
                        if on_result_ready:
                            on_result_ready(rec)

                log(
                    f"   📊 [NGOBOX Summary] Keyword '{keyword}': {len(cards)} listed, "
                    f"{n_title_miss} title-miss, {n_neg} negative, {n_stale} stale, "
                    f"{n_dup} duplicates, {len(results)} saved"
                )

            except Exception as e:
                log(f"   ❌ [NGOBOX] Search error: {e}")
            finally:
                browser.close()

        return results

    def _extract_detail(self, ctx, url, keyword, title, published, base_dir, log, db=None, max_age_hours: int = 24, team_id: str = "tmi"):
        detail = ctx.new_page()
        try:
            detail.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                detail.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            detail.wait_for_timeout(1500)

            # Extract body text
            try:
                body_text = detail.locator("body").text_content() or ""
                body_text = re.sub(r"[ \t]+", " ", body_text)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            except Exception:
                body_text = ""

            neg = find_negative_keyword(title, body_text, team_id=team_id)
            if neg:
                log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on detail page")
                if db:
                    db.mark_downloaded(title, url, "ngobox", keyword, "", team_id=team_id)
                return None

            # Date extraction if missing from listing card
            if not published and body_text:
                published = extract_date_from_text(body_text) or ""
            if not published and body_text:
                m = re.search(
                    r'\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|'
                    r'Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|'
                    r'Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},?\s+\d{4})',
                    body_text, re.IGNORECASE
                )
                if m:
                    published = m.group(1).strip()

            if published:
                if not is_deadline_active(published):
                    log(f"      📅 Skipping '{title[:60]}' — deadline expired ({published})")
                    return None
                log(f"      ✅ Deadline active: {published}")

            # Create tender folder
            safe_title = re.sub(r'[\\/*?:"<>|]', "_", title)[:40].strip("_. ")
            tender_dir = base_dir / safe_title
            tender_dir.mkdir(parents=True, exist_ok=True)

            # Find document download links (e.g. "click here", ".pdf", ".docx")
            doc_links = detail.evaluate("""
            () => {
                const links = Array.from(document.querySelectorAll('a[href]'));
                const EXCLUDE = ['tariff', 'ads_plans', 'whatsapp.com', 'facebook.com', 'twitter.com', 'linkedin.com', 'post_announcement'];
                return links
                    .map(a => ({
                        text: a.textContent.trim(),
                        href: a.href,
                        parentText: a.parentElement ? a.parentElement.textContent.trim() : ''
                    }))
                    .filter(item => {
                        if (!item.href || item.href.startsWith('#') || item.href.startsWith('javascript:')) return false;
                        if (item.href === window.location.href || item.href.includes('full_rfp_eoi_') || item.href.includes('rfp_eoi_listing')) return false;
                        const lowerHref = item.href.toLowerCase();
                        if (EXCLUDE.some(ex => lowerHref.includes(ex))) return false;

                        const lowerText = item.text.toLowerCase();
                        const lowerParent = item.parentText.toLowerCase();

                        const isDocExt = lowerHref.endsWith('.pdf') || lowerHref.endsWith('.doc') || lowerHref.endsWith('.docx') || lowerHref.includes('/media/');
                        const isClickHere = lowerText.includes('click here') || lowerParent.includes('click here') || lowerParent.includes('download') || lowerParent.includes('detailed eoi') || lowerParent.includes('full rfp');

                        return isDocExt || isClickHere;
                    });
            }
            """)

            downloaded_files = []
            seen_files = set()

            for link in doc_links:
                file_url = link["href"]
                if file_url in seen_files:
                    continue
                seen_files.add(file_url)

                try:
                    log(f"      ⬇️ Downloading detailed document: {file_url[:70]}")
                    # Attempt download via Playwright page.expect_download or HTTP request
                    try:
                        with detail.expect_download(timeout=10000) as download_info:
                            detail.evaluate(f"url => window.location.href = url", file_url)
                        download = download_info.value
                        dest = tender_dir / download.suggested_filename
                        download.save_as(dest)
                        downloaded_files.append(str(dest))
                        log(f"      ✅ Downloaded: {dest.name}")
                    except Exception:
                        # Fallback: direct HTTP download via python request if Playwright download trigger didn't fire
                        import urllib.request
                        raw_fname = os.path.basename(file_url.split("?")[0]) or "document.pdf"
                        raw_fname = re.sub(r'[#?].*$', '', raw_fname)
                        raw_fname = re.sub(r'[\\/*?:"<>|]', '_', raw_fname)
                        ext = ".pdf"
                        for e in ['.pdf', '.doc', '.docx', '.xlsx']:
                            if raw_fname.lower().endswith(e):
                                ext = e
                                raw_fname = raw_fname[:-len(e)]
                                break
                        fname = raw_fname[:40].strip("_. ") + ext
                        dest = tender_dir / fname
                        req = urllib.request.Request(file_url, headers={"User-Agent": "Mozilla/5.0"})
                        with urllib.request.urlopen(req, timeout=15) as resp, open(dest, "wb") as out_f:
                            out_f.write(resp.read())
                        downloaded_files.append(str(dest))
                        log(f"      ✅ Direct HTTP Downloaded: {dest.name}")
                except Exception as de:
                    log(f"      ⚠️ Failed to download file from {file_url[:50]}: {de}")

            # Mark in DB with NGOBOX detail page URL (not PDF link)
            if db:
                db.mark_downloaded(title, url, "ngobox", keyword, published, team_id=team_id)

            return {
                "keyword": keyword,
                "title": title,
                "url": url,  # NGOBOX detail notice page link preserved!
                "published": published,
                "page_text": body_text,
                "files": downloaded_files,
                "tender_dir": str(tender_dir),
            }

        except Exception as e:
            log(f"      ❌ Failed to extract detail for '{title[:50]}': {e}")
            return None
        finally:
            detail.close()
