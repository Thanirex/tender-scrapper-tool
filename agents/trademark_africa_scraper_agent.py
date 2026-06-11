import re
import sys
import html as html_mod
import requests
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from paths import DOWNLOADS_DIR

RESULTS_CAP = 20


class TradeMarkAfricaScraperAgent:
    API_URL = "https://trademarkafrica.com/wp-json/wp/v2/posts"

    def search(self, keyword, output_dir=None, log_callback=None, on_result_ready=None, db=None):
        def log(msg):
            if log_callback:
                log_callback(msg)
            else:
                print(msg)

        results = []
        log(f"🔍 [TradeMark Africa] Scanning for '{keyword}'...")

        try:
            posts = self._fetch_posts(keyword, log)
            log(f"   ↳ {len(posts)} post(s) returned by API")

            for post in posts:
                title = html_mod.unescape(post.get("title", {}).get("rendered", "")).strip()
                url   = post.get("link", "")

                if not title or not url:
                    continue

                if keyword.lower() not in title.lower():
                    continue

                if db and db.is_duplicate(title, url):
                    log(f"   ⏩ Duplicate: {title[:60]}")
                    continue

                log(f"   📄 {title[:70]}")
                base = Path(output_dir) if output_dir else DOWNLOADS_DIR / "trademarkafrica"
                rec  = self._process_post(post, keyword, base, log, db)
                if rec:
                    results.append(rec)
                    if on_result_ready:
                        on_result_ready(rec)

        except Exception as e:
            log(f"❌ TradeMark Africa scrape error: {e}")

        return results

    def _fetch_posts(self, keyword: str, log) -> list:
        try:
            resp = requests.get(
                self.API_URL,
                params={
                    "search":   keyword,
                    "per_page": RESULTS_CAP,
                    "_fields":  "id,title,link,date,excerpt,content",
                },
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log(f"   ⚠️ API error: {e}")
            return []

    def _process_post(self, post: dict, keyword: str, base_dir: Path, log, db=None) -> dict | None:
        title   = html_mod.unescape(post.get("title",   {}).get("rendered", "")).strip()
        url     = post.get("link", "")
        content = post.get("content", {}).get("rendered", "")
        excerpt = post.get("excerpt", {}).get("rendered", "")

        # Strip HTML tags → plain text
        body_text = re.sub(r"<[^>]+>", " ", content)
        body_text = html_mod.unescape(body_text)
        body_text = re.sub(r"\s+", " ", body_text).strip()

        # Extract deadline from excerpt
        deadline = ""
        exc_text = re.sub(r"<[^>]+>", " ", excerpt)
        m = re.search(r"deadline[:\s]+([^\n<]{5,80})", exc_text, re.IGNORECASE)
        if m:
            deadline = m.group(1).strip().rstrip(".")

        # Extract all document URLs from raw HTML (PDF, DOCX, DOC, XLSX)
        doc_urls = list(dict.fromkeys(re.findall(
            r'https://(?:www\.)?trademarkafrica\.com/wp-content/uploads/[^\s"\'<>]+'
            r'\.(?:pdf|docx?|xlsx?)',
            content, re.IGNORECASE,
        )))

        log(f"      📎 {len(doc_urls)} document(s) found")

        safe_kw    = re.sub(r'[\\/*?:"<>|\s]', "_", keyword)[:20]
        safe_title = re.sub(r'[\\/*?:"<>|]',   "_", title)[:35].strip("_. ")
        tender_dir = base_dir / safe_kw / safe_title
        tender_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        for doc_url in doc_urls:
            try:
                fname     = doc_url.rstrip("/").split("/")[-1].split("?")[0]
                safe_stem = re.sub(r'[\\/*?:"<>|]', "_", Path(fname).stem)[:55]
                safe_ext  = Path(fname).suffix[:10]
                out_path  = tender_dir / (safe_stem + safe_ext)

                r = requests.get(doc_url, timeout=60, stream=True)
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(8192):
                        f.write(chunk)
                downloaded.append(str(out_path))
                log(f"      💾 {safe_stem + safe_ext}")
            except Exception as dl_err:
                log(f"      ⚠️ Download failed ({doc_url}): {dl_err}")

        # Save full page content as text
        full_text = f"Title: {title}\nDeadline: {deadline}\nURL: {url}\n\n{body_text}"
        txt_path  = tender_dir / "page_content.txt"
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(full_text)
        if not downloaded:
            downloaded.append(str(txt_path))
            log(f"      📝 No documents — saved page text")
        else:
            log(f"      📝 Page text saved alongside {len(downloaded)} document(s)")

        if db:
            db.mark_downloaded(title, url, "trademarkafrica", keyword, deadline)

        return {
            "keyword":    keyword,
            "title":      title,
            "url":        url,
            "page_text":  full_text,
            "files":      downloaded,
            "tender_dir": str(tender_dir),
            "deadline":   deadline,
            "site":       "trademarkafrica",
        }
