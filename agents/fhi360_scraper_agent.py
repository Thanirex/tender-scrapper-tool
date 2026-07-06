import re
import sys
import requests
from pathlib import Path
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR
from keyword_utils import keyword_matches

_JS_DIAG = """
() => ({
    title:       document.title,
    hrCount:     document.querySelectorAll('hr').length,
    strongCount: document.querySelectorAll('strong').length,
    bCount:      document.querySelectorAll('b').length,
    aCount:      document.querySelectorAll('a[href]').length,
    firstB:      (document.querySelector('b') || {textContent:''}).textContent.trim().slice(0, 80),
    hrParentTag: document.querySelector('hr') ? document.querySelector('hr').parentElement.tagName : 'none',
    hrParentId:  document.querySelector('hr') ? (document.querySelector('hr').parentElement.id || '') : '',
    hrParentCls: document.querySelector('hr') ? (document.querySelector('hr').parentElement.className || '') : '',
})
"""

_JS_COLLECT = """
() => {
    // Use the parent of the first <hr> as the content container —
    // avoids guessing class/ID and always lands at the right level.
    const hrs = Array.from(document.querySelectorAll('hr'));
    if (!hrs.length) return [];

    const container = hrs[0].parentElement;
    if (!container) return [];

    // Split container's direct children at every <HR> boundary
    const blocks = [];
    let current  = [];
    for (const child of container.children) {
        if (child.tagName === 'HR') {
            if (current.length) { blocks.push([...current]); current = []; }
        } else {
            current.push(child);
        }
    }
    if (current.length) blocks.push(current);

    return blocks.map(nodes => {
        const tmp = document.createElement('div');
        nodes.forEach(n => tmp.appendChild(n.cloneNode(true)));

        // ── Title detection ─────────────────────────────────────────────
        // Priority: <b> tag → <strong> tag → first non-metadata line of text
        let title = '';

        for (const tag of ['b', 'strong']) {
            const el = tmp.querySelector(tag);
            if (el) {
                const t = el.textContent.trim();
                if (t.length >= 8) { title = t; break; }
            }
        }

        if (!title) {
            // Walk every child and take the first line that isn't metadata
            const SKIP = /^(rfp|rfq|issue\\s+date|clos|deadline|no\\.|file|modification|download)/i;
            for (const child of tmp.children) {
                // Handle <br>-separated text inside a single block element
                const raw = child.innerHTML
                    ? child.innerHTML.split(/<br\\s*\\/?>/i)[0].replace(/<[^>]+>/g, '').trim()
                    : child.textContent.trim();
                if (!raw || raw.length < 8 || SKIP.test(raw)) continue;
                // Skip lines that are only a link
                const onlyLink = child.children.length === 1 &&
                                 child.children[0].tagName === 'A' &&
                                 child.textContent.trim() === child.children[0].textContent.trim();
                if (!onlyLink) { title = raw.slice(0, 200); break; }
            }
        }

        if (!title) return null;

        // ── File links ───────────────────────────────────────────────────
        const links = [...new Set(
            Array.from(tmp.querySelectorAll('a[href]'))
                .map(a => a.href)
                .filter(h => /\\.(pdf|xlsx|xls|docx|doc|zip)(\\?|$)/i.test(h))
        )];

        // ── Metadata ─────────────────────────────────────────────────────
        const fullText = tmp.textContent.replace(/\\s+/g, ' ').trim();
        const closeM   = fullText.match(/clos(?:ing|e)\\s+date[:\\s]+([\\d\\/\\-]+)/i);
        const issueM   = fullText.match(/issue\\s+date[:\\s]+([\\d\\/\\-]+)/i);
        const refM     = fullText.match(/RFP\\/RFQ\\s*No\\.?[:\\s]+([\\w\\-\\/]+)/i);

        return {
            title,
            links,
            text:         fullText,
            issue_date:   issueM ? issueM[1] : '',
            closing_date: closeM ? closeM[1] : '',
            ref_no:       refM   ? refM[1]   : '',
        };
    }).filter(Boolean);
}
"""


class FHI360ScraperAgent:
    PAGE_URL = "https://solicitations.fhi360.org/Solicitation.aspx"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [FHI 360] Scanning for '{keyword}'...")

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            try:
                page.goto(self.PAGE_URL, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(2000)

                diag = page.evaluate(_JS_DIAG)
                log(
                    f"   🔍 hr={diag.get('hrCount')} b={diag.get('bCount')} "
                    f"strong={diag.get('strongCount')} links={diag.get('aCount')} | "
                    f"hr-parent=<{diag.get('hrParentTag')} id='{diag.get('hrParentId')}' "
                    f"class='{diag.get('hrParentCls', '')[:40]}'> | "
                    f"first-b='{diag.get('firstB', '')}'"
                )

                solicitations = self._collect_solicitations(page, log)
                log(f"   ↳ {len(solicitations)} solicitation(s) on page")

                # Show sample so problems are immediately visible
                for sol in solicitations[:3]:
                    log(f"   • '{sol['title'][:65]}' | files={len(sol['links'])}")

                base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "fhi360"

                for sol in solicitations:
                    title = sol["title"]
                    text  = sol["text"]
                    links = sol["links"]

                    if not keyword_matches(keyword, title, text):
                        continue

                    url = links[0] if links else self.PAGE_URL

                    if db and db.is_duplicate(title, url):
                        log(f"   ⏩ Duplicate: {title[:60]}")
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

                    if text:
                        txt_path = tender_dir / "page_content.txt"
                        with open(txt_path, "w", encoding="utf-8") as f:
                            f.write(f"Source: {self.PAGE_URL}\n\n{text}")
                        if not downloaded:
                            downloaded.append(str(txt_path))
                            log(f"      📝 No files — saved solicitation text only")
                        else:
                            log(f"      📝 Text saved alongside files")

                    if db:
                        db.mark_downloaded(title, url, "fhi360", keyword, sol.get("closing_date", ""))

                    rec = {
                        "keyword":    keyword,
                        "title":      title,
                        "url":        url,
                        "page_text":  text,
                        "files":      downloaded,
                        "tender_dir": str(tender_dir),
                        "site":       "fhi360",
                    }
                    results.append(rec)
                    if on_result_ready:
                        on_result_ready(rec)

            except Exception as e:
                log(f"❌ FHI 360 scrape error: {e}")
            finally:
                browser.close()

        return results

    def _collect_solicitations(self, page, log) -> list:
        try:
            return page.evaluate(_JS_COLLECT) or []
        except Exception as e:
            log(f"   ⚠️ Error parsing solicitations: {e}")
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
            log(f"      ⚠️ Download failed ({url}): {e}")
            return None
