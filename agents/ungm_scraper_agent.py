import os
import re
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# Lift this number to raise the per-keyword tender cap
RESULTS_CAP = 10


class UNGMScraperAgent:
    BASE_URL = "https://www.ungm.org"
    LOGIN_URL = "https://www.ungm.org/Login"
    NOTICES_URL = "https://www.ungm.org/Public/Notice"

    def scrape(self, email: str, password: str, keywords: list, run_dir: str,
               headless: bool = True, log_callback=None, on_tender_ready=None) -> list:
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
            browser = p.chromium.launch(headless=headless, slow_mo=0)
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            try:
                if not self._login(page, email, password, log):
                    return []

                for keyword in keywords:
                    log(f"▶️ Keyword: '{keyword}'")
                    kw_results = self._search_keyword(page, ctx, keyword, run_dir, log, on_tender_ready)
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

    def _search_keyword(self, page, ctx, keyword: str, run_dir: str, log, on_tender_ready=None) -> list:
        if not self._goto(page, self.NOTICES_URL, log, wait_for="input#txtNoticeFilterTitle"):
            return []

        # Order matters: check Active FIRST and let its AJAX settle,
        # THEN type the keyword, THEN click Search.
        # Checking Active fires its own AJAX — if done after typing it overwrites the keyword.
        try:
            cb = page.locator("input#chkIsActive")
            cb.wait_for(state="visible", timeout=5000)
            if not cb.is_checked():
                cb.check()
            log("   ✓ Active-only filter enabled")
            # Wait for the AJAX triggered by the checkbox to settle
            page.wait_for_load_state("networkidle", timeout=5000)
        except Exception as e:
            log(f"   ℹ️ Active-only checkbox not found: {e}")

        try:
            title_input = page.locator("input#txtNoticeFilterTitle")
            title_input.wait_for(state="visible", timeout=10000)
            title_input.click()
            title_input.press("Control+a")
            # press_sequentially fires real keyboard events — fill() bypasses them on AJAX forms
            title_input.press_sequentially(keyword, delay=40)
        except Exception as e:
            log(f"   ❌ Could not fill keyword: {e}")
            return []

        try:
            page.locator("button#lnkSearch").click()
            # Wait for either results table or empty-notice div to become visible
            try:
                page.locator("#tblNotices, #noticesEmpty").first.wait_for(state="visible", timeout=15000)
            except Exception:
                page.wait_for_timeout(8000)
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

        hrefs = raw_hrefs[:RESULTS_CAP]

        if not hrefs:
            log(f"   ↳ No results found")
            return []

        log(f"   ↳ Opening {len(hrefs)} tenders (cap={RESULTS_CAP})")

        results = []
        for idx, href in enumerate(hrefs):
            log(f"   📄 [{idx+1}/{len(hrefs)}] {href}")
            rec = self._extract_tender(page, ctx, href, keyword, run_dir, log)
            if rec:
                results.append(rec)
                if on_tender_ready:
                    on_tender_ready(rec)   # summarise + save Excel immediately

        return results

    _ERROR_SIGNALS = {
        "internal server error", "404", "not found", "access denied",
        "forbidden", "page not found", "error 500", "bad request",
    }

    def _extract_tender(self, page, ctx, url: str, keyword: str, run_dir: str, log) -> dict | None:
        """
        Open the notice in a FRESH TAB so the search-results page stays intact.
        The original `page` is never navigated away — only used for keyword search.
        """
        notice_page = ctx.new_page()
        try:
            log(f"      🔄 Opening notice page...")
            try:
                notice_page.goto(url, wait_until="domcontentloaded", timeout=45000)
            except Exception as nav_e:
                log(f"      ❌ Navigation error: {nav_e}")
                return None

            # Wait for the heading to confirm real content loaded
            try:
                notice_page.locator("h1").wait_for(state="visible", timeout=10000)
            except Exception:
                log(f"      ⚠️ Page did not render (h1 not visible after 10 s) — skipping.")
                return None

            # Session-expired: UNGM redirects silently to /Login
            if "/Login" in notice_page.url or "/login" in notice_page.url:
                log(f"      ⚠️ Session expired — redirected to login. Skipping.")
                return None

            # Title
            try:
                title = notice_page.locator("h1").first.text_content().strip()
            except Exception:
                title = url.rstrip("/").split("/")[-1]

            # Bail out on error pages — skip rather than waste an LLM call on garbage
            if any(sig in title.lower() for sig in self._ERROR_SIGNALS):
                log(f"      ⚠️ Error page ('{title}') — skipping.")
                return None

            log(f"      📋 {title[:70]}")

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

            # Folder for this tender (short paths — Windows MAX_PATH = 260)
            safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
            safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
            tender_dir = Path(run_dir) / safe_kw / safe_title
            tender_dir.mkdir(parents=True, exist_ok=True)

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
            downloaded = []
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
