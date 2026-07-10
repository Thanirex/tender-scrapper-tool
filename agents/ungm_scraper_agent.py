import os
import re
import sys
import platform
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from date_utils import is_within_24h_ist, extract_date_from_text
from keyword_utils import keyword_matches, find_negative_keyword

class UNGMScraperAgent:
    BASE_URL = "https://www.ungm.org"
    LOGIN_URL = "https://www.ungm.org/Login"
    NOTICES_URL = "https://www.ungm.org/Public/Notice"

    def scrape(self, email: str, password: str, keywords: list, run_dir: str,
               headless: bool = True, log_callback=None, on_tender_ready=None,
               db=None) -> list:
        """
        Full UNGM flow: login → per-keyword search → extract → download.
        on_tender_ready(rec): optional callback fired immediately after each tender
        is downloaded — use it to summarise and save while the next download runs.
        Returns list of all result dicts.
        """
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
                if not self._login(page, email, password, log):
                    return []

                for keyword in keywords:
                    log(f"▶️ Keyword: '{keyword}'")
                    kw_results = self._search_keyword(page, ctx, keyword, run_dir, log, on_tender_ready, db)
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

    def _login(self, page, email: str, password: str, log) -> bool:
        log("🔐 Opening UNGM login page...")
        if not self._goto(page, self.LOGIN_URL, log, wait_for="input#UserName"):
            return False

        try:
            email_input = page.locator("input#UserName")
            email_input.wait_for(state="visible", timeout=15000)
            email_input.fill(email)
            page.locator("input#Password").fill(password)
            page.locator("button:has-text('Log in')").click()
            # Wait for redirect away from /Login rather than sleeping a fixed time
            try:
                page.wait_for_url(lambda u: "/Login" not in u, timeout=10000)
            except Exception:
                pass
        except Exception as e:
            log(f"❌ Login form error: {e}")
            return False

        if "/Login" in page.url:
            log("❌ Login failed — check credentials.")
            return False

        log("✅ Logged in.")
        return True

    def _search_keyword(self, page, ctx, keyword: str, run_dir: str, log, on_tender_ready=None, db=None) -> list:
        if not self._goto(page, self.NOTICES_URL, log, wait_for="input#txtNoticeFilterTitle"):
            return []

        # Order matters: check Active FIRST and let its AJAX settle,
        # THEN type the keyword, THEN click Search.
        # Checking Active fires its own AJAX — if done after typing it overwrites the keyword.
        try:
            cb = page.locator("input#chkIsActive")
            cb.wait_for(state="visible", timeout=5000)
            if not cb.is_checked():
                # Use expect_response so we exit as soon as the AJAX call returns,
                # instead of waiting for networkidle which UNGM never reaches.
                try:
                    with page.expect_response(
                        lambda r: "/Public/Notice" in r.url and r.status == 200,
                        timeout=6000
                    ):
                        cb.check()
                except Exception:
                    page.wait_for_timeout(1000)
            log("   ✓ Active-only filter enabled")
        except Exception as e:
            log(f"   ℹ️ Active-only checkbox not found: {e}")

        try:
            title_input = page.locator("input#txtNoticeFilterTitle")
            title_input.wait_for(state="visible", timeout=10000)
            title_input.click()
            title_input.press("Control+a")
            # press_sequentially fires real keyboard events — fill() bypasses them on AJAX forms
            title_input.press_sequentially(keyword, delay=20)
        except Exception as e:
            log(f"   ❌ Could not fill keyword: {e}")
            return []

        try:
            # Wrap the click in expect_response so we exit as soon as the search
            # AJAX returns — avoids the 8-second fallback wait_for_timeout.
            try:
                with page.expect_response(
                    lambda r: "/Public/Notice" in r.url and r.status == 200,
                    timeout=12000
                ):
                    page.locator("button#lnkSearch").click()
            except Exception:
                page.wait_for_timeout(2000)
            log(f"   🔍 Search submitted for '{keyword}'")
        except Exception as e:
            log(f"   ❌ Search submission failed: {e}")
            return []

        # If the empty-results notice is visible, the search found nothing — stop here
        try:
            if page.locator("#noticesEmpty").is_visible():
                log(f"   ↳ No results for '{keyword}'")
                return []
        except Exception:
            pass

        try:
            total_label = page.locator("#noticeSearchTotal").text_content().strip()
            log(f"   ↳ UNGM reports {total_label} results for '{keyword}'")
        except Exception:
            pass

        # Collect all notice hrefs in ONE JS call — no per-element round trips
        try:
            raw_hrefs = page.eval_on_selector_all(
                "#tblNotices a[href*='/Public/Notice/']",
                "els => [...new Set(els.map(e => e.href))].filter(h => /\\/Public\\/Notice\\/\\d+/.test(h))"
            )
        except Exception:
            raw_hrefs = []

        hrefs = raw_hrefs

        if not hrefs:
            log(f"   ↳ No results found")
            return []

        log(f"   ↳ Opening {len(hrefs)} tenders")

        results = []
        stats = {"error": 0, "title_miss": 0, "neg": 0, "stale": 0, "no_date": 0, "dup": 0}
        for idx, href in enumerate(hrefs):
            log(f"   📄 [{idx+1}/{len(hrefs)}] {href}")
            rec = self._extract_tender(page, ctx, href, keyword, run_dir, log, db, stats=stats)
            if rec:
                results.append(rec)
                if on_tender_ready:
                    on_tender_ready(rec)   # summarise + save Excel immediately

        log(
            f"   📊 '{keyword}' summary on UNGM: {len(hrefs)} notice(s) opened → "
            f"{stats['title_miss']} without the keyword in the title, "
            f"{stats['neg']} blocked by negative keywords, {stats['stale']} older than 24h, "
            f"{stats['no_date']} missing a publish date, {stats['dup']} already collected, "
            f"{stats['error']} failed to load, {len(results)} saved"
        )

        return results

    _ERROR_SIGNALS = {
        "internal server error", "404", "not found", "access denied",
        "forbidden", "page not found", "error 500", "bad request",
    }

    def _extract_tender(self, page, ctx, url: str, keyword: str, run_dir: str, log, db=None, stats=None) -> dict | None:
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
                log(f"      ⚠️ Page did not render (h1 not visible after 10 s) — skipping.")
                return None

            # Session-expired: UNGM redirects silently to /Login
            if "/Login" in notice_page.url or "/login" in notice_page.url:
                _bump("error")
                log(f"      ⚠️ Session expired — redirected to login. Skipping.")
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

            neg = find_negative_keyword(title)
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

            neg = find_negative_keyword(title, body_text)
            if neg:
                _bump("neg")
                log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on page")
                if db:
                    db.mark_downloaded(title, url, "ungm", keyword, "")
                return None

            # ── Date filter: last 24 hours IST ──────────────────────────────
            # Try verified "Published on" field first; fall back to body text.
            pub_date = next(
                (v for k, v in verified.items() if "published" in k.lower()),
                None,
            )
            if not pub_date:
                pub_date = extract_date_from_text(body_text)

            if pub_date:
                if not is_within_24h_ist(pub_date):
                    _bump("stale")
                    log(f"      📅 Skipping '{title[:60]}' — matched, but published {pub_date} (>24h ago)")
                    return None
            else:
                _bump("no_date")
                log(f"      ⚠️ No publication date found for '{title[:60]}' — skipping")
                return None

            # ── Deduplication check ──────────────────────────────────────────
            if db and db.is_duplicate(title, url):
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
                            fname = dl.suggested_filename or "quantum_documents.zip"
                            stem = Path(fname).stem[:55]
                            ext  = Path(fname).suffix[:10]
                            safe_fname = re.sub(r'[\\/*?:"<>|]', "_", stem) + ext
                            out_path = tender_dir / safe_fname
                            dl.save_as(str(out_path))
                            downloaded.append(str(out_path))
                            log(f"      💾 Quantum Documents downloaded: {safe_fname}")
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
                    fname = dl.suggested_filename or "attachment"
                    stem = Path(fname).stem[:55]
                    ext  = Path(fname).suffix[:10]
                    safe_fname = re.sub(r'[\\/*?:"<>|]', "_", stem) + ext
                    out_path = tender_dir / safe_fname
                    dl.save_as(str(out_path))
                    downloaded.append(str(out_path))
                    log(f"      💾 {safe_fname}")
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
                db.mark_downloaded(title, url, "ungm", keyword, pub_date or "")

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
