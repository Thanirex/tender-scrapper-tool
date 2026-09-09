import os
import re
import sys
import platform
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from date_utils import (is_within_cutoff_ist, extract_date_from_text, get_max_age_hours,
                        is_date_or_deadline_valid, now_ist_naive)
from keyword_utils import keyword_matches, find_negative_keyword

class UNGMScraperAgent:
    """Scrapes UNGM's public procurement notices.

    UNGM requires no account to read /Public/Notice: the listing is served by a
    JSON-in / HTML-out endpoint (SEARCH_URL) that answers anonymously, and each
    notice detail page is public. The agent therefore carries no credentials.

    The listing is fetched from SEARCH_URL directly rather than by driving the
    filter form. UNGM renders results as ARIA grid divs (`data-noticeid`), not a
    real <table>, so the old `table#tblNotices tbody tr` selector matched
    nothing and every run reported zero notices.
    """

    BASE_URL = "https://www.ungm.org"
    NOTICES_URL = "https://www.ungm.org/Public/Notice"
    SEARCH_URL = "https://www.ungm.org/Public/Notice/Search"

    # Rows come back as <div role="row" ... data-noticeid="311446" ...>
    _NOTICE_ID_RE = re.compile(r'data-noticeid="(\d+)"')

    # Notices requested per keyword. UNGM caps a page at 15 server-side and
    # ignores anything larger, so this matches what the endpoint will actually
    # return. Results are sorted by soonest deadline, so the 15 are the most
    # urgent open opportunities — the same page the UI itself shows.
    PAGE_SIZE = 15

    def scrape(self, keywords: list, run_dir: str,
               headless: bool = True, log_callback=None, on_tender_ready=None,
               db=None, team_id: str = "cnk", max_age_hours: int | None = None) -> list:
        """Per-keyword public search → extract → download.

        on_tender_ready(rec): optional callback fired immediately after each
        tender is downloaded — use it to summarise and save while the next
        download runs. Returns list of all result dicts.
        """
        if max_age_hours is None:
            max_age_hours = get_max_age_hours(team_id)

        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        all_results = []

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(headless=headless, slow_mo=0)
            except Exception as launch_err:
                if not headless:
                    log(f"⚠️ Headed launch failed ({type(launch_err).__name__}: {launch_err!r})")
                    log("   ↳ Tip: run  playwright install chromium  in your local Python env")
                    log("   ↳ Falling back to headless mode...")
                    browser = p.chromium.launch(headless=True, slow_mo=0)
                else:
                    raise
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            try:
                # One visit to the public listing so the context picks up the
                # cookies UNGM's search endpoint expects.
                self._goto(page, self.NOTICES_URL, log, wait_for="input#txtNoticeFilterTitle")

                for keyword in keywords:
                    log(f"▶️ Keyword: '{keyword}'")
                    kw_results = self._search_keyword(page, ctx, keyword, run_dir, log, on_tender_ready, db, max_age_hours=max_age_hours, team_id=team_id)
                    all_results.extend(kw_results)
                    log(f"   ↳ {len(kw_results)} tenders processed for '{keyword}'")

            except Exception as e:
                log(f"❌ Unexpected error: {e}")
            finally:
                browser.close()

        return all_results

    def _goto(self, page, url: str, log, wait_for: str = None) -> bool:
        """
        Navigate and wait until a specific element appears (or 3s max).
        wait_for: CSS selector to wait for — avoids fixed sleeps.
        """
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if wait_for:
                page.locator(wait_for).wait_for(state="visible", timeout=8000)
            else:
                page.wait_for_timeout(2000)
            return True
        except Exception as e:
            log(f"❌ Navigation failed ({url}): {e}")
            return False

    def _search_payload(self, keyword: str) -> dict:
        """Mirror of the payload UNGM's own filter form posts.

        IsActive + DeadlineFrom=today keep the result set to opportunities that
        are still open, which is the filter the UI calls "Active only".
        """
        today = now_ist_naive().strftime("%d-%b-%Y")
        return {
            "PageIndex": 0,
            "PageSize": self.PAGE_SIZE,
            "Title": keyword,
            "Description": "",
            "Reference": "",
            "PublishedFrom": "",
            "PublishedTo": today,
            "DeadlineFrom": today,
            "DeadlineTo": "",
            "Countries": [],
            "Agencies": [],
            "UNSPSCs": [],
            "NoticeTypes": [],
            "SortField": "Deadline",
            "SortAscending": True,
            "isPicker": False,
            "IsSustainable": False,
            "IsActive": True,
            "NoticeDisplayType": None,
            "NoticeSearchTotalLabelId": "noticeSearchTotal",
            "TypeOfCompetitions": [],
        }

    def _fetch_notice_ids(self, ctx, keyword: str, log) -> list:
        """POST the public search endpoint and pull notice ids out of the HTML.

        Going straight to the endpoint avoids driving UNGM's AJAX filter form,
        which needed a keystroke-by-keystroke dance and a settle wait per
        keyword. The response is an HTML fragment of ARIA grid rows.
        """
        try:
            resp = ctx.request.post(
                self.SEARCH_URL,
                data=self._search_payload(keyword),
                headers={
                    "Content-Type": "application/json",
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": self.NOTICES_URL,
                },
                timeout=45000,
            )
        except Exception as e:
            log(f"   ❌ Search request failed: {e}")
            return []

        if not resp.ok:
            log(f"   ❌ Search returned HTTP {resp.status}")
            return []

        try:
            html = resp.text()
        except Exception as e:
            log(f"   ❌ Could not read search response: {e}")
            return []

        ids, seen = [], set()
        for nid in self._NOTICE_ID_RE.findall(html):
            if nid not in seen:
                seen.add(nid)
                ids.append(nid)
        return ids

    def _search_keyword(self, page, ctx, keyword: str, run_dir: str, log, on_tender_ready=None, db=None, max_age_hours: int = 24, team_id: str = "cnk") -> list:
        notice_ids = self._fetch_notice_ids(ctx, keyword, log)
        if not notice_ids:
            log(f"   ↳ No results for '{keyword}'")
            log(
                f"   📊 '{keyword}' summary on UNGM: 0 notice(s) opened → "
                f"0 without the keyword in the title, 0 blocked by negative keywords, "
                f"0 older than {max_age_hours}h, 0 missing a publish date, "
                f"0 already collected, 0 failed to load, 0 saved"
            )
            return []

        hrefs = [f"{self.BASE_URL}/Public/Notice/{nid}" for nid in notice_ids]
        log(f"   ↳ {len(hrefs)} active notice(s) matched '{keyword}'")

        log(f"   ↳ Opening {len(hrefs)} tenders")

        results = []
        stats = {"error": 0, "title_miss": 0, "neg": 0, "stale": 0, "no_date": 0, "dup": 0}
        for idx, href in enumerate(hrefs):
            log(f"   📄 [{idx+1}/{len(hrefs)}] {href}")
            rec = self._extract_tender(page, ctx, href, keyword, run_dir, log, db, stats=stats, max_age_hours=max_age_hours, team_id=team_id)
            if rec:
                results.append(rec)
                if on_tender_ready:
                    on_tender_ready(rec)   # summarise + save Excel immediately

        log(
            f"   📊 '{keyword}' summary on UNGM: {len(hrefs)} notice(s) opened → "
            f"{stats['title_miss']} without the keyword in the title, "
            f"{stats['neg']} blocked by negative keywords, {stats['stale']} older than {max_age_hours}h, "
            f"{stats['no_date']} missing a publish date, {stats['dup']} already collected, "
            f"{stats['error']} failed to load, {len(results)} saved"
        )

        return results

    _ERROR_SIGNALS = {
        "internal server error", "404", "not found", "access denied",
        "forbidden", "page not found", "error 500", "bad request",
    }

    # Tender folders are zipped and served to users by /download/tender, so an
    # executable must never be written into one. UNGM answers some document
    # links with an empty placeholder carrying an .exe name; whatever the
    # reason, the extension is refused on the way in rather than trusted.
    _BLOCKED_EXTS = {
        ".exe", ".scr", ".bat", ".cmd", ".com", ".pif", ".msi", ".msp",
        ".vbs", ".vbe", ".js", ".jse", ".wsf", ".wsh", ".ps1", ".psm1",
        ".jar", ".dll", ".cpl", ".hta", ".reg", ".lnk", ".app",
    }

    def _save_download(self, dl, tender_dir: Path, log, fallback: str = "attachment") -> "str | None":
        """Persist a Playwright download, refusing executables and empty files."""
        fname = dl.suggested_filename or fallback
        stem  = Path(fname).stem[:55]
        ext   = Path(fname).suffix[:10]

        if ext.lower() in self._BLOCKED_EXTS:
            log(f"      ⛔ Skipped '{fname}' — executable file type is not downloaded")
            return None

        safe_fname = re.sub(r'[\\/*?:"<>|]', "_", stem) + ext
        out_path   = tender_dir / safe_fname
        dl.save_as(str(out_path))

        # UNGM hands back a 0-byte body for documents that need a session.
        # An empty file is noise in the tender folder, so drop it.
        try:
            if out_path.stat().st_size == 0:
                out_path.unlink()
                log(f"      ⚠️ '{safe_fname}' came back empty — not saved")
                return None
        except OSError:
            pass

        log(f"      💾 {safe_fname}")
        return str(out_path)

    def _extract_tender(self, page, ctx, url: str, keyword: str, run_dir: str, log, db=None, stats=None, max_age_hours: int = 24, team_id: str = "cnk") -> dict | None:
        """
        Open the notice in a FRESH TAB so the search-results page stays intact.
        The original `page` is never navigated away — only used for keyword search.
        """
        def _bump(reason: str):
            if stats is not None:
                stats[reason] = stats.get(reason, 0) + 1

        notice_page = ctx.new_page()
        try:
            log(f"      🔄 Opening notice page...")
            try:
                notice_page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as nav_e:
                _bump("error")
                log(f"      ❌ Navigation error: {nav_e}")
                return None

            # Wait for the heading to confirm real content loaded
            try:
                notice_page.locator("h1").wait_for(state="visible", timeout=10000)
            except Exception:
                _bump("error")
                log(f"      ❌ Notice page did not render (h1 missing) — skipping")
                return None

            # Public notices never require an account; a redirect to /Login means
            # this particular notice is restricted, so skip it rather than fail.
            if "/Login" in notice_page.url or "/login" in notice_page.url:
                _bump("error")
                log(f"      ⚠️ Notice is not public — skipping.")
                return None

            # Title
            try:
                title = notice_page.locator("h1").first.text_content().strip()
            except Exception:
                title = url.rstrip("/").split("/")[-1]

            # Bail out on error pages — skip rather than waste an LLM call on garbage
            if any(sig in title.lower() for sig in self._ERROR_SIGNALS):
                _bump("error")
                log(f"      ⚠️ Error page ('{title}') — skipping.")
                return None

            log(f"      📋 {title[:70]}")

            # ── Keyword relevance check ──────────────────────────────────────
            if not keyword_matches(keyword, title):
                _bump("title_miss")
                log(f"      🚫 Skipping '{title[:60]}' — keyword '{keyword}' not in title")
                return None

            neg = find_negative_keyword(title, team_id=team_id)
            if neg:
                _bump("neg")
                log(f"      🚫 Skipping '{title[:60]}' — negative keyword '{neg}' in title")
                return None

            # Verified structured fields (ground truth — no LLM needed)
            verified = {}
            try:
                for lbl_el in notice_page.locator("span.label").all():
                    try:
                        label = lbl_el.text_content().strip().rstrip(":")
                        parent_text = lbl_el.locator("..").text_content().strip()
                        value = parent_text.replace(lbl_el.text_content().strip(), "").strip()
                        if label and value:
                            verified[label] = value
                    except Exception:
                        continue
            except Exception:
                pass

            if verified:
                log(f"      ✅ {len(verified)} verified fields scraped")

            # Full visible page text
            try:
                body_text = notice_page.locator("body").text_content()
                body_text = re.sub(r"[ \t]+", " ", body_text)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            except Exception:
                body_text = ""

            neg = find_negative_keyword(title, body_text, team_id=team_id)
            if neg:
                _bump("neg")
                log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on page")
                if db:
                    db.mark_downloaded(title, url, "ungm", keyword, "", team_id=team_id)
                return None

            # ── Date filter: active publication or deadline ─────────────────
            pub_date = next(
                (v for k, v in verified.items() if "published" in k.lower()),
                None,
            )
            deadline_date = next(
                (v for k, v in verified.items() if "deadline" in k.lower() or "express" in k.lower()),
                None,
            )
            date_to_check = deadline_date if deadline_date else pub_date
            if not date_to_check:
                date_to_check = extract_date_from_text(body_text)

            if date_to_check:
                if not is_date_or_deadline_valid(date_to_check, max_age_hours):
                    _bump("stale")
                    log(f"      📅 Skipping '{title[:60]}' — date/deadline {date_to_check} expired")
                    return None
            else:
                _bump("no_date")
                log(f"      ⚠️ No publication date found for '{title[:60]}' — skipping")
                return None

            # ── Deduplication check ──────────────────────────────────────────
            if db and db.is_duplicate(title, url, team_id=team_id):
                _bump("dup")
                log(f"      ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                return None

            # Folder for this tender (short paths — Windows MAX_PATH = 260)
            safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
            safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
            tender_dir = Path(run_dir) / safe_kw / safe_title
            tender_dir.mkdir(parents=True, exist_ok=True)

            downloaded = []

            # Check for Quantum links
            try:
                # UNGM sometimes hides links under a tab. Let's try to click the Links tab if it exists.
                try:
                    # Look for tabs containing "Links"
                    links_tabs = notice_page.locator("a, button, span").filter(has_text=re.compile(r"^Links$", re.IGNORECASE)).all()
                    for tab in links_tabs:
                        if tab.is_visible():
                            tab.click()
                            notice_page.wait_for_timeout(1500)
                            break
                except Exception as e:
                    pass
                
                # Extract URLs
                token_url = None
                quantum_url = None

                # Method 1: Look at table rows directly (UNGM typically uses this structure for Links)
                try:
                    for row in notice_page.locator("table tr").all():
                        try:
                            # all_inner_texts() on td returns a list of text contents for each cell
                            cells = row.locator("td").all_inner_texts()
                            if len(cells) >= 2:
                                url_text = cells[0].strip()
                                desc_text = cells[1].strip().lower()
                                
                                if url_text.startswith("http"):
                                    if "click on this link before accessing" in desc_text:
                                        token_url = url_text
                                    elif not quantum_url and "negotiation document" in desc_text:
                                        # Use 'not quantum_url' to grab the first match and ignore 'Direct link to Quantum...'
                                        quantum_url = url_text
                        except Exception:
                            continue
                except Exception:
                    pass

                # Method 2: Fallback to evaluating JS for 'a' tags if the table structure didn't match
                if not token_url or not quantum_url:
                    links_data = notice_page.evaluate("""() => {
                        return Array.from(document.querySelectorAll('a')).map(a => {
                            let parent = a.closest('tr') || a.closest('div.row') || a.parentElement;
                            return {
                                href: a.href || '',
                                text: a.textContent.trim(),
                                parentText: parent ? parent.textContent.trim() : ''
                            };
                        });
                    }""")
                    
                    for l in links_data:
                        a_text = l["text"].lower()
                        p_text = l["parentText"].lower()
                        
                        if "go to link" in a_text or "direct link" in a_text or "direct link" in p_text:
                            if not token_url and "click on this link before accessing" in p_text:
                                token_url = l["href"]
                            elif not quantum_url and "negotiation document" in p_text:
                                quantum_url = l["href"]
                
                if token_url and quantum_url:
                    log("      🔑 Found Quantum token and negotiation links. Processing Quantum flow...")
                    
                    # 1. Open token URL
                    token_page = ctx.new_page()
                    try:
                        token_page.goto(token_url, wait_until="domcontentloaded", timeout=60000)
                        # Wait for the document ID to appear (e.g. UNDPPUBDOCS-)
                        doc_link = token_page.locator("a:has-text('UNDPPUBDOCS-')").first
                        try:
                            doc_link.wait_for(state="visible", timeout=30000)
                            
                            # Clicking it opens a new tab
                            with ctx.expect_page() as new_page_info:
                                doc_link.click()
                            security_page = new_page_info.value
                            security_page.wait_for_load_state("domcontentloaded")
                            security_page.wait_for_timeout(3000)  # Wait for token to register
                            security_page.close()
                            log("      ✅ Security token acquired")
                        except Exception as e:
                            log(f"      ⚠️ Failed to acquire security token: {e}")
                    except Exception as e:
                        log(f"      ⚠️ Error in token page: {e}")
                    finally:
                        token_page.close()
                        
                    # 2. Open Quantum Negotiation link
                    quantum_page = ctx.new_page()
                    try:
                        quantum_page.goto(quantum_url, wait_until="domcontentloaded", timeout=60000)
                        quantum_page.wait_for_timeout(5000)  # Wait for SharePoint/Oracle to load
                        
                        # Select all documents
                        try:
                            # Attempt 1: SharePoint supports Ctrl+A to select all items
                            quantum_page.keyboard.press("Control+a")
                            quantum_page.wait_for_timeout(1000)
                            
                            download_btn = quantum_page.locator("button[name='Download'], [data-automationid='downloadCommand'], button:has-text('Download'), a:has-text('Download')").first
                            
                            # Attempt 2: Click the 'Select All' header checkbox
                            if not download_btn.is_visible():
                                select_all_header = quantum_page.locator("div[data-automationid='toggleSelection'], [aria-label*='Toggle selection' i], [aria-label*='Select all' i]").first
                                if select_all_header.is_visible():
                                    select_all_header.click()
                                    log("      ☑️ Clicked 'Select All' header")
                                    quantum_page.wait_for_timeout(1000)

                            # Attempt 3: Hover each row to reveal the hidden 'check_' box and click it
                            if not download_btn.is_visible():
                                rows = quantum_page.locator("div[data-automationid='DetailsRow'], .ms-DetailsRow, [role='row']").all()
                                clicked_count = 0
                                for row in rows:
                                    if row.is_visible() and "header" not in row.get_attribute("class").lower():
                                        try:
                                            # Hovering reveals the check circle
                                            row.hover()
                                            quantum_page.wait_for_timeout(150)
                                            # Look for check element or left-most selection column
                                            check_btn = row.locator("[class*='check_'], [role='checkbox'], i[data-icon-name='CircleRing'], .ms-DetailsRow-check").first
                                            if check_btn.is_visible():
                                                check_btn.click()
                                                clicked_count += 1
                                            else:
                                                # Click the extreme left edge of the row as fallback
                                                row.click(position={"x": 5, "y": 10})
                                                clicked_count += 1
                                        except Exception:
                                            pass
                                log(f"      ☑️ Hovered and clicked {clicked_count} row checkboxes")
                                
                            quantum_page.wait_for_timeout(1500)
                        except Exception as e:
                            log(f"      ⚠️ Error selecting documents: {e}")
                            
                        # Then click Download
                        try:
                            download_btn.wait_for(state="visible", timeout=15000)
                            
                            with quantum_page.expect_download(timeout=120000) as dl_info:
                                download_btn.click()
                            
                            dl = dl_info.value
                            saved = self._save_download(dl, tender_dir, log,
                                                        fallback="quantum_documents.zip")
                            if saved:
                                downloaded.append(saved)
                                log(f"      💾 Quantum Documents downloaded: {Path(saved).name}")
                        except Exception as e:
                            log(f"      ⚠️ Error downloading Quantum documents: {e}")
                            
                    except Exception as e:
                        log(f"      ⚠️ Error in Quantum negotiation page: {e}")
                    finally:
                        quantum_page.close()
            except Exception as e:
                log(f"      ⚠️ Error processing Quantum links: {e}")

            # Collect all download hrefs before clicking any
            att_hrefs = []
            try:
                for att_el in notice_page.locator("a[href*='DownloadDocument']").all():
                    href = att_el.get_attribute("href") or ""
                    if href:
                        if not href.startswith("http"):
                            href = self.BASE_URL + href
                        att_hrefs.append(href)
            except Exception:
                pass

            if att_hrefs:
                log(f"      📎 {len(att_hrefs)} attachment(s) found")

            # Download each attachment in its own tab
            for att_href in att_hrefs:
                dl_page = None
                try:
                    dl_page = ctx.new_page()
                    with dl_page.expect_download(timeout=30000) as dl_info:
                        try:
                            dl_page.goto(att_href, wait_until="commit", timeout=30000)
                        except Exception:
                            pass
                    dl = dl_info.value
                    saved = self._save_download(dl, tender_dir, log)
                    if saved:
                        downloaded.append(saved)
                except PlaywrightTimeout:
                    log(f"      ⚠️ Download timed out")
                except Exception as dl_e:
                    log(f"      ⚠️ Download error: {dl_e}")
                finally:
                    if dl_page:
                        try:
                            dl_page.close()
                        except Exception:
                            pass

            # --- Extract all ZIP files recursively ---
            # Extract member-by-member with truncated filenames to avoid Windows
            # MAX_PATH (260-char) errors when zip entries have long original names.
            try:
                import zipfile
                while True:
                    zip_files = list(tender_dir.rglob("*.zip"))
                    if not zip_files:
                        break
                    extracted_any = False
                    for zf in zip_files:
                        try:
                            with zipfile.ZipFile(zf, 'r') as zip_ref:
                                for member in zip_ref.infolist():
                                    if member.filename.endswith('/'):
                                        continue
                                    member_p = Path(member.filename)
                                    safe_stem = re.sub(r'[\*?:"<>|]', "_", member_p.stem)[:80]
                                    safe_ext  = re.sub(r'[\*?:"<>|]', "_", member_p.suffix)[:10]
                                    out_file  = tender_dir / (safe_stem + safe_ext)
                                    out_file.write_bytes(zip_ref.read(member))
                                log(f"      📦 Extracted zip: {zf.name}")
                            zf.unlink()
                            extracted_any = True
                        except Exception as e:
                            log(f"      ⚠️ Error extracting {zf.name}: {e}")
                            try:
                                zf.unlink()
                            except Exception:
                                pass

                    if not extracted_any:
                        break
                        
                # Update downloaded list to reflect extracted files
                downloaded = [str(f) for f in tender_dir.rglob("*") if f.is_file() and f.name != "page_content.txt"]
            except Exception as e:
                log(f"      ⚠️ Error during unzip process: {e}")

            # Always save page text to disk for archiving.
            # Only add to `files` when there are no other attachments — api.py already sends
            # page_text to the LLM separately, so adding it again would duplicate content.
            if body_text:
                txt_path = tender_dir / "page_content.txt"
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"Source: {url}\n\n{body_text}")
                if not downloaded:
                    downloaded.append(str(txt_path))
                    log(f"      📝 No attachments — saved page text as page_content.txt")
                else:
                    log(f"      📝 Page content saved to disk alongside documents")

            # Mark in DB so subsequent keywords don't re-download this tender
            if db:
                db.mark_downloaded(title, url, "ungm", keyword, pub_date or "", team_id=team_id)

            return {
                "keyword": keyword,
                "title": title,
                "url": url,
                "page_text": body_text,
                "files": downloaded,
                "verified": verified,
                "tender_dir": str(tender_dir),
            }

        except Exception as e:
            log(f"      ❌ Extraction error on {url}: {e}")
            return None
        finally:
            try:
                notice_page.close()
            except Exception:
                pass
