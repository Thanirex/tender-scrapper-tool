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

    def scrape(self, email: str, password: str, keywords: list, run_dir: str, headless: bool = True, log_callback=None) -> list:
        """
        Full UNGM flow: login → per-keyword search → extract → download.
        Returns list of dicts: {keyword, title, url, page_text, files:[paths]}
        Set headless=False to watch the browser live on screen.
        """
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        all_results = []

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=headless, slow_mo=100 if not headless else 0)
            ctx = browser.new_context(accept_downloads=True)
            page = ctx.new_page()
            try:
                if not self._login(page, email, password, log):
                    return []

                for keyword in keywords:
                    log(f"▶️ Keyword: '{keyword}'")
                    # Pass ctx so _extract_tender can open separate pages for downloads
                    kw_results = self._search_keyword(page, ctx, keyword, run_dir, log)
                    all_results.extend(kw_results)
                    log(f"   ↳ {len(kw_results)} tenders processed for '{keyword}'")

            except Exception as e:
                log(f"❌ Unexpected error: {e}")
            finally:
                browser.close()

        return all_results

    def _goto(self, page, url: str, log) -> bool:
        """Navigate and wait for the page to visually settle."""
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(4000)
            return True
        except Exception as e:
            log(f"❌ Navigation failed ({url}): {e}")
            return False

    def _login(self, page, email: str, password: str, log) -> bool:
        log("🔐 Opening UNGM login page...")
        if not self._goto(page, self.LOGIN_URL, log):
            return False

        try:
            email_input = page.locator("input#UserName")
            email_input.wait_for(state="visible", timeout=15000)
            email_input.fill(email)
            page.locator("input#Password").fill(password)
            page.locator("button:has-text('Log in')").click()
            page.wait_for_timeout(4000)
        except Exception as e:
            log(f"❌ Login form error: {e}")
            return False

        if "/Login" in page.url:
            log("❌ Login failed — check credentials.")
            return False

        log("✅ Logged in.")
        return True

    def _search_keyword(self, page, ctx, keyword: str, run_dir: str, log) -> list:
        if not self._goto(page, self.NOTICES_URL, log):
            return []

        # Order matters: check Active FIRST and let its AJAX settle,
        # THEN type the keyword, THEN click Search.
        # Checking Active fires its own AJAX — if done after typing it overwrites the keyword.
        try:
            cb = page.locator("input#chkIsActive")
            cb.wait_for(state="visible", timeout=10000)
            if not cb.is_checked():
                cb.check()
            log("   ✓ Active-only filter enabled")
            page.wait_for_timeout(3000)
        except Exception as e:
            log(f"   ℹ️ Active-only checkbox not found: {e}")

        try:
            title_input = page.locator("input#txtNoticeFilterTitle")
            title_input.wait_for(state="visible", timeout=10000)
            title_input.click()
            title_input.press("Control+a")
            # press_sequentially fires real keyboard events — fill() bypasses them on AJAX forms
            title_input.press_sequentially(keyword, delay=40)
            page.wait_for_timeout(500)
        except Exception as e:
            log(f"   ❌ Could not fill keyword: {e}")
            return []

        try:
            page.locator("button#lnkSearch").click()
            page.wait_for_timeout(10000)  # UNGM AJAX needs ~8s to return filtered results
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

        # Scope links to #tblNotices only — avoids picking up stale DOM links
        # left behind by the Active-filter AJAX that fired earlier
        try:
            link_els = page.locator("#tblNotices a[href*='/Public/Notice/']").all()
        except Exception:
            link_els = []

        hrefs = []
        for el in link_els:
            try:
                href = el.get_attribute("href") or ""
                if re.search(r"/Public/Notice/\d+", href):
                    if not href.startswith("http"):
                        href = self.BASE_URL + href
                    if href not in hrefs:
                        hrefs.append(href)
                        if len(hrefs) >= RESULTS_CAP:
                            break
            except Exception:
                continue

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

        return results

    def _extract_tender(self, page, ctx, url: str, keyword: str, run_dir: str, log) -> dict | None:
        if not self._goto(page, url, log):
            return None

        try:
            # Title
            try:
                title = page.locator("h1").first.text_content().strip()
            except Exception:
                title = url.rstrip("/").split("/")[-1]

            # Full visible page text
            try:
                body_text = page.locator("body").text_content()
                body_text = re.sub(r"[ \t]+", " ", body_text)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            except Exception:
                body_text = ""

            # Short paths — Windows MAX_PATH is 260 chars
            safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
            safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
            tender_dir = Path(run_dir) / safe_kw / safe_title
            tender_dir.mkdir(parents=True, exist_ok=True)

            # Collect all download hrefs BEFORE touching any of them
            # (clicking a download link on the main page can navigate it away)
            att_hrefs = []
            try:
                att_els = page.locator("a[href*='DownloadDocument']").all()
                for att_el in att_els:
                    href = att_el.get_attribute("href") or ""
                    if href:
                        if not href.startswith("http"):
                            href = self.BASE_URL + href
                        att_hrefs.append(href)
            except Exception:
                pass

            # Download each file on a SEPARATE page so the main page stays alive
            downloaded = []
            for att_href in att_hrefs:
                dl_page = None
                try:
                    dl_page = ctx.new_page()
                    with dl_page.expect_download(timeout=30000) as dl_info:
                        try:
                            # goto() raises "Download is starting" for file responses —
                            # that is expected; the download is still caught by expect_download
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

            # If no attachments exist, save page text as a .txt file so the
            # summarizer has something to read from the folder
            if not downloaded and body_text:
                txt_path = tender_dir / "page_content.txt"
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"Source: {url}\n\n{body_text}")
                downloaded.append(str(txt_path))
                log(f"      📝 No attachments — saved page text as page_content.txt")

            return {
                "keyword": keyword,
                "title": title,
                "url": url,
                "page_text": body_text,
                "files": downloaded,
            }

        except Exception as e:
            log(f"   ❌ Extraction error on {url}: {e}")
            return None
