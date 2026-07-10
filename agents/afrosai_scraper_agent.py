import re
import sys
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches, find_negative_keyword

# Page is built with Elementor. Each tender entry sits in its own inner
# container (e-con / elementor-column) which holds sibling widgets:
#   elementor-widget-image | elementor-widget-heading | elementor-widget-text-editor | elementor-widget-button
# Strategy: find each h2 inside a heading widget → walk up to the widget div
# → its parentElement is the per-tender container → collect all links there.
# Falls back to h2-sibling walk for plain WordPress pages.
_JS_COLLECT = """
() => {
    const results = [];
    const seen = new Set();

    // ── Elementor-aware strategy ─────────────────────────────────────────
    const headingWidgets = Array.from(
        document.querySelectorAll('.elementor-widget-heading')
    );

    if (headingWidgets.length) {
        for (const widget of headingWidgets) {
            const h2 = widget.querySelector('h2, h3');
            if (!h2) continue;
            const title = h2.textContent.trim();
            if (!title || title.length < 5 || seen.has(title)) continue;
            seen.add(title);

            // The widget's parent is the per-tender inner column
            const tenderContainer = widget.parentElement;
            if (!tenderContainer) continue;

            const links = [];
            tenderContainer.querySelectorAll('a[href]').forEach(a => {
                const h = a.href || '';
                // Accept direct file links OR any button/link whose text suggests download
                const isFile = /\\.(pdf|docx?|xlsx?|zip)(\\?|$)/i.test(h);
                const isDlBtn = /download|click here|view.*full|rfp|eoi/i.test(
                    a.textContent.trim()
                );
                if ((isFile || isDlBtn) && h && !h.endsWith('#') && !links.includes(h))
                    links.push(h);
            });

            const fullText = tenderContainer.textContent.replace(/\\s+/g, ' ').trim();
            const deadlineM = fullText.match(/(?:submission\\s+)?deadline[:\\s]+([^.\\n]{5,40})/i);

            results.push({
                title,
                links,
                text: fullText,
                deadline: deadlineM ? deadlineM[1].trim() : '',
            });
        }
        if (results.length) return results;
    }

    // ── Fallback: plain h2-sibling walk ─────────────────────────────────
    const container = (
        document.querySelector('.entry-content') ||
        document.querySelector('main') ||
        document.querySelector('article') ||
        document.querySelector('#content') ||
        document.body
    );

    const h2s = Array.from(container.querySelectorAll('h2'));
    for (let i = 0; i < h2s.length; i++) {
        const h2    = h2s[i];
        const nextH2 = h2s[i + 1] || null;
        const title  = h2.textContent.trim();
        if (!title || title.length < 5 || seen.has(title)) continue;
        seen.add(title);

        const links = [];
        const textParts = [title];

        let node = h2.nextElementSibling;
        while (node) {
            if (nextH2 && (node === nextH2 || node.contains(nextH2))) break;
            const nodeText = node.textContent.trim();
            if (nodeText) textParts.push(nodeText);
            node.querySelectorAll('a[href]').forEach(a => {
                const h = a.href || '';
                if (/\\.(pdf|docx?|xlsx?|zip)(\\?|$)/i.test(h) && !links.includes(h))
                    links.push(h);
            });
            node = node.nextElementSibling;
        }

        const fullText = textParts.join(' ').replace(/\\s+/g, ' ').trim();
        const deadlineM = fullText.match(/(?:submission\\s+)?deadline[:\\s]+([^.\\n]{5,40})/i);
        results.push({ title, links, text: fullText, deadline: deadlineM ? deadlineM[1].trim() : '' });
    }
    return results;
}
"""


class AfrosaiScraperAgent:
    PAGE_URL = "https://afrosai-e.org.za/tenders/"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [AFROSAI-E] Scanning for '{keyword}'...")

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
                page.wait_for_timeout(1500)

                tenders = self._collect_tenders(page, log)
                log(f"   ↳ {len(tenders)} tender entry(ies) on page")
                for t in tenders[:3]:
                    log(f"   • '{t['title'][:65]}' | files={len(t['links'])}")

                base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "afrosai"

                n_miss = n_neg = n_dup = 0
                for tender in tenders:
                    title = tender["title"]
                    text  = tender["text"]
                    links = tender["links"]

                    if not keyword_matches(keyword, title, text):
                        n_miss += 1
                        continue

                    neg = find_negative_keyword(title, text)
                    if neg:
                        n_neg += 1
                        log(f"   🚫 Skipping '{title[:60]}' — negative keyword '{neg}'")
                        continue

                    url = links[0] if links else self.PAGE_URL

                    if db and db.is_duplicate(title, url):
                        n_dup += 1
                        log(f"   ⏩ Duplicate: '{title[:60]}' — already collected in an earlier run")
                        continue

                    log(f"   📄 {title[:70]}")

                    safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
                    safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
                    tender_dir = base / safe_kw / safe_title
                    tender_dir.mkdir(parents=True, exist_ok=True)

                    downloaded = []
                    for link in links:
                        saved = self._download_file(link, tender_dir, log)
                        if saved:
                            downloaded.append(saved)

                    # Always save page text for the summarizer
                    if text:
                        txt_path = tender_dir / "page_content.txt"
                        with open(txt_path, "w", encoding="utf-8") as f:
                            f.write(f"Source: {self.PAGE_URL}\n\n{text}")
                        if not downloaded:
                            downloaded.append(str(txt_path))
                            log(f"      📝 No documents — saved entry text only")
                        else:
                            log(f"      📝 Entry text also saved")

                    if db:
                        db.mark_downloaded(title, url, "afrosai", keyword, tender.get("deadline", ""))

                    rec = {
                        "keyword":    keyword,
                        "title":      title,
                        "url":        url,
                        "page_text":  text,
                        "files":      downloaded,
                        "tender_dir": str(tender_dir),
                        "site":       "afrosai",
                        "deadline":   tender.get("deadline", ""),
                    }
                    results.append(rec)
                    if on_result_ready:
                        on_result_ready(rec)

                log(
                    f"   📊 '{keyword}' summary on AFROSAI-E: {len(tenders)} entry(ies) listed → "
                    f"{n_miss} without the keyword, {n_neg} blocked by negative keywords, "
                    f"{n_dup} already collected, {len(results)} saved"
                )

            except Exception as e:
                log(f"❌ AFROSAI-E scrape error: {e}")
            finally:
                browser.close()

        return results

    def _collect_tenders(self, page, log) -> list:
        try:
            tenders = page.evaluate(_JS_COLLECT) or []
            return tenders
        except Exception as e:
            log(f"   ⚠️ Error parsing tender entries: {e}")
            return []

    def _download_file(self, url: str, tender_dir: Path, log) -> "str | None":
        try:
            fname     = url.rstrip("/").split("/")[-1].split("?")[0]
            if not fname or "." not in fname:
                fname = "document.pdf"
            safe_stem = re.sub(r'[\\/*?:"<>|]', "_", Path(fname).stem)[:55]
            safe_ext  = Path(fname).suffix[:10] or ".pdf"
            out_path  = tender_dir / (safe_stem + safe_ext)

            resp = requests.get(url, timeout=45, stream=True,
                                headers={"User-Agent": "Mozilla/5.0"})
            resp.raise_for_status()

            if "text/html" in resp.headers.get("content-type", ""):
                log(f"      ⚠️ HTML response for {fname} — skipping")
                return None

            with open(out_path, "wb") as f:
                for chunk in resp.iter_content(8192):
                    f.write(chunk)
            log(f"      💾 {safe_stem + safe_ext}")
            return str(out_path)
        except Exception as e:
            log(f"      ⚠️ Download failed ({url[-60:]}): {e}")
            return None
