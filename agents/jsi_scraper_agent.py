import re
import sys
import requests
from pathlib import Path
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from date_utils import get_max_age_hours
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches, find_negative_keyword


def _is_due_date_active(due_str: str) -> bool:
    """Return True if the due date is in the future, or if we can't parse it."""
    if not due_str:
        return True
    for fmt in ("%b %d, %Y, %I:%M %p", "%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            dt = datetime.strptime(due_str.strip(), fmt).replace(tzinfo=timezone.utc)
            return dt >= datetime.now(tz=timezone.utc)
        except ValueError:
            continue
    return True  # unparseable → give benefit of the doubt


class JSIScraperAgent:
    BASE_URL    = "https://www.jsi.org"
    LISTING_URL = "https://www.jsi.org/partner-with-jsi/solicitations/"

    def __init__(self):
        self._cached_entries = None

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None, team_id="cnk", max_age_hours=None):
        if max_age_hours is None:
            max_age_hours = get_max_age_hours(team_id)

        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [JSI] Scanning for '{keyword}'...")

        if self._cached_entries is not None:
            entries = self._cached_entries
            log(f"   ↳ {len(entries)} solicitation link(s) (using cached listing)")
        else:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx = browser.new_context(
                    accept_downloads=True,
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/124.0.0.0 Safari/537.36"
                    ),
                )
                page = ctx.new_page()
                try:
                    page.goto(self.LISTING_URL, wait_until="domcontentloaded", timeout=45000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PlaywrightTimeout:
                        pass
                    page.wait_for_timeout(1500)
                    entries = page.evaluate("""
                    () => {
                        const BASE = "https://www.jsi.org";
                        const seen = new Set();
                        const results = [];
                        document.querySelectorAll('a[href*="/solicitation/"]').forEach(a => {
                            let href = a.getAttribute('href') || '';
                            if (!href) return;
                            if (href.replace(/\\/$/, '').endsWith('/solicitations')) return;
                            if (href.startsWith('/')) href = BASE + href;
                            if (seen.has(href)) return;
                            seen.add(href);

                            let title = a.textContent.trim();
                            if (!title || title.length < 5) {
                                let node = a.parentElement;
                                for (let i = 0; i < 4 && node; i++) {
                                    const h = node.querySelector('h1,h2,h3,h4');
                                    if (h) { title = h.textContent.trim(); break; }
                                    title = node.textContent.trim().split('\\n')[0].trim();
                                    if (title.length > 5) break;
                                    node = node.parentElement;
                                }
                            }
                            if (title) results.push({ title, url: href });
                        });
                        return results;
                    }
                    """) or []
                    self._cached_entries = entries
                    log(f"   ↳ {len(entries)} solicitation link(s) on listing page")
                except Exception as e:
                    log(f"❌ [JSI] Fetch error: {e}")
                    entries = []
                finally:
                    browser.close()

        base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "jsi"

        n_title_miss = n_neg = n_dup = n_opened = 0
        for entry in entries:
            title = entry["title"]
            url   = entry["url"]

            if not keyword_matches(keyword, title):
                n_title_miss += 1
                continue

            neg = find_negative_keyword(title, team_id=team_id)
            if neg:
                n_neg += 1
                log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}' in title")
                continue

            if db and db.is_duplicate(title, url, team_id=team_id):
                n_dup += 1
                log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                continue

            n_opened += 1
            log(f"   📄 Opening: {title[:70]}")
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                ctx     = browser.new_context(accept_downloads=True)
                try:
                    rec = self._extract_detail(ctx, url, keyword, title, base, log, db, team_id=team_id)
                    if rec:
                        results.append(rec)
                        if on_result_ready:
                            on_result_ready(rec)
                finally:
                    browser.close()

        log(
            f"   📊 '{keyword}' summary on JSI: {len(entries)} item(s) listed → "
            f"{n_title_miss} without the keyword in the title, "
            f"{n_neg} blocked by negative keywords, {n_dup} already collected, "
            f"{n_opened} opened for full check, {len(results)} saved"
        )

        return results

    # ── Detail page ────────────────────────────────────────────────────────

    def _extract_detail(self, ctx, url, keyword, list_title, base_dir, log, db=None, team_id="cnk"):
        detail = ctx.new_page()
        try:
            detail.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                detail.wait_for_load_state("networkidle", timeout=10000)
            except PlaywrightTimeout:
                pass
            detail.wait_for_timeout(1500)

            # ── Title (more accurate than listing text) ───────────────────
            title = list_title
            for sel in ["h1", "h2.entry-title", "h2"]:
                try:
                    t = detail.locator(sel).first.text_content(timeout=3000).strip()
                    if t and len(t) > 5:
                        title = t
                        break
                except Exception:
                    pass

            # ── Full page text ────────────────────────────────────────────
            try:
                body_text = detail.locator("body").text_content() or ""
                body_text = re.sub(r"[ \t]+", " ", body_text)
                body_text = re.sub(r"\n{3,}", "\n\n", body_text).strip()
            except Exception:
                body_text = ""

            neg = find_negative_keyword(title, body_text, team_id=team_id)
            if neg:
                log(f"      🚫 Rejected '{title[:60]}' — negative keyword '{neg}' found on page")
                if db:
                    db.mark_downloaded(title, url, "jsi", keyword, "", team_id=team_id)
                return None

            # ── Active check: parse "Due Date" from page text ─────────────
            due_match = re.search(
                r'due\s+date\s*[:\-]?\s*([A-Za-z]{3,9}\.?\s+\d{1,2},?\s+\d{4}(?:,\s*\d{1,2}:\d{2}\s*[AP]M)?)',
                body_text, re.IGNORECASE
            )
            due_str = due_match.group(1).strip() if due_match else ""
            if due_str and not _is_due_date_active(due_str):
                log(f"      ⏭ Solicitation closed (due {due_str}): {title[:55]}")
                return None

            # ── Collect RFP / document links ──────────────────────────────
            doc_links = detail.evaluate("""
            () => {
                const seen = new Set();
                const links = [];
                document.querySelectorAll('a[href]').forEach(a => {
                    const h = a.href || '';
                    const t = a.textContent.trim();
                    if (!h || seen.has(h)) return;
                    // Accept: JSI solicitations CDN, PDF/doc files, links labelled RFP/download
                    const isSolCDN = /solicitations\\.jsi\\.(com|org)/i.test(h);
                    const isFile   = /\\.(pdf|docx?|xlsx?|zip)(\\?|$)/i.test(h);
                    const isRFP    = /rfp|rfq|download|solicitation/i.test(t);
                    if (isSolCDN || isFile || isRFP) {
                        seen.add(h);
                        links.push({ href: h, text: t });
                    }
                });
                return links;
            }
            """) or []

            log(f"      📎 {len(doc_links)} document link(s) found")
            for d in doc_links:
                log(f"         → '{d['text'][:50]}' — {d['href'][-70:]}")

            safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
            safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
            tender_dir = base_dir / safe_kw / safe_title
            tender_dir.mkdir(parents=True, exist_ok=True)

            downloaded = []
            for doc in doc_links:
                saved = self._download_doc(doc["href"], tender_dir, log)
                if saved:
                    downloaded.append(saved)

            # Save page text alongside documents
            if body_text:
                txt_path = tender_dir / "page_content.txt"
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(f"Source: {url}\n\n{body_text}")
                if not downloaded:
                    downloaded.append(str(txt_path))
                    log(f"      📝 No files — saved page text only")
                else:
                    log(f"      📝 Page text also saved")

            if db:
                db.mark_downloaded(title, url, "jsi", keyword, due_str)

            return {
                "keyword":    keyword,
                "title":      title,
                "url":        url,
                "page_text":  body_text,
                "files":      downloaded,
                "tender_dir": str(tender_dir),
                "site":       "jsi",
                "deadline":   due_str,
            }

        except Exception as e:
            log(f"      ❌ Extraction error: {e}")
            return None
        finally:
            try:
                detail.close()
            except Exception:
                pass

    # ── Download helper ────────────────────────────────────────────────────

    def _download_doc(self, url: str, tender_dir: Path, log) -> "str | None":
        try:
            resp = requests.get(
                url, timeout=60, stream=True, allow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0",
                    "Accept": "application/pdf,application/octet-stream,*/*",
                },
            )
            resp.raise_for_status()

            content_type = resp.headers.get("content-type", "")
            if "text/html" in content_type:
                log(f"      ⚠️ HTML response for {url[-50:]} — skipping")
                return None

            # Determine filename from Content-Disposition or URL
            fname = ""
            cd = resp.headers.get("content-disposition", "")
            m = re.search(r'filename\*?=(?:UTF-8\'\')?["\']?([^"\';\r\n]+)', cd, re.IGNORECASE)
            if m:
                fname = m.group(1).strip().strip('"\'')
            if not fname:
                fname = url.rstrip("/").split("/")[-1].split("?")[0]
            if not fname or "." not in fname:
                # Guess extension from content-type
                ext = ".pdf" if "pdf" in content_type else ".zip" if "zip" in content_type else ".bin"
                fname = "document" + ext

            safe_stem = re.sub(r'[\\/*?:"<>|]', "_", Path(fname).stem)[:55]
            safe_ext  = Path(fname).suffix[:10] or ".pdf"
            out_path  = tender_dir / (safe_stem + safe_ext)

            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            log(f"      💾 {safe_stem + safe_ext}")
            return str(out_path)
        except Exception as e:
            log(f"      ⚠️ Download failed ({url[-60:]}): {e}")
            return None
