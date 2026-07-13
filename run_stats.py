"""Run statistics collector — turns agent log streams into report-card numbers.

Every scraper agent already counts why each listed result was kept or
rejected, but only prints those counts into the run log. This module watches
the same log stream and aggregates the numbers per site so they can be shown
as report cards on the dashboard, TAiQ page, and manual-scraper page.

The agents' log lines are a CONTRACT (see SCRAPING_CONSTRAINTS.md):
  🚫 ... not in title            → keyword missing from the title
  🚫 ... negative keyword ...    → blocked by a negative keyword
  📅 ...                         → published more than 24h ago
  ⚠️ No publication date / No date in URL → no publish date found
  ⏭ ...                         → tender archived / expired / closed
  ⏩ ...                         → duplicate (already collected)
  ⚠️ Error on row / Row error    → row-level scrape error
  📊 '<kw>' summary on <SITE>: N ... → per-keyword rollup with counts in prose

Some agents skip results silently (no per-line log) but always report the
count in their 📊 summary line. The collector therefore counts per-line
markers as they stream in and, when a summary line arrives, reconciles each
reason with `max(per-line count, summary count)` for that keyword block —
so a reason is never missed and never double-counted.

`saved` is NOT parsed from logs: the runner calls record_saved() exactly
when a tender is written to the database.
"""

import re
import threading

# Rejection reasons, in the order report cards display them
REASON_KEYS = ("title_miss", "negative", "stale", "no_date",
               "closed", "duplicate", "error")

REASON_LABELS = {
    "title_miss": "keyword not in title",
    "negative":   "blocked by negative keywords",
    "stale":      "older than 24 hours",
    "no_date":    "no publish date found",
    "closed":     "archived or expired",
    "duplicate":  "already collected earlier",
    "error":      "errored while scraping",
}

# Phrases used in the agents' 📊 summary lines, mapped to reasons
_SUMMARY_PHRASES = {
    "title_miss": re.compile(r"(\d+)\s+without the keyword"),
    "negative":   re.compile(r"(\d+)\s+blocked by negative keywords"),
    "stale":      re.compile(r"(\d+)\s+older than 24h"),
    "no_date":    re.compile(r"(\d+)\s+missing a (?:publish )?date"),
    "closed":     re.compile(r"(\d+)\s+archived/expired"),
    "duplicate":  re.compile(r"(\d+)\s+already collected"),
    "error":      re.compile(r"(\d+)\s+errored"),
}

_SUMMARY_LISTED = re.compile(r"summary on [^:]+:\s*(\d+)")
_KEYWORD_LINE   = re.compile(r"Keyword:\s*'(.+?)'")
# Cron-runner lines: site phase transitions and per-tender saves
_PHASE_LINE     = re.compile(r"🌐 Phase \d+\s*/\s*\d+ — (\S+)")
_SAVED_MARK     = "✅ Saved tender #"


def _new_site() -> dict:
    return {
        "listed": 0,
        "saved": 0,
        "keywords_done": 0,
        "rejected": {k: 0 for k in REASON_KEYS},
    }


class RunStatsCollector:
    """Aggregates per-site scrape statistics from a shared log stream.

    Feed every log line through feed(); call set_site() when the run moves
    to another website and record_saved() when a tender is persisted.
    All methods are safe to call from the scraper worker thread.
    """

    def __init__(self, site: str = ""):
        self._lock  = threading.Lock()
        self._sites: dict = {}
        self._site  = ""
        self.current_keyword = ""
        if site:
            self.set_site(site)
        # Per-line tallies for the current keyword block, reconciled against
        # the 📊 summary line so silent skips are still counted once.
        self._block: dict = {k: 0 for k in REASON_KEYS}

    # ── run wiring ─────────────────────────────────────────────────────────

    def set_site(self, site: str):
        site = (site or "").strip().lower()
        if not site:
            return
        with self._lock:
            self._flush_block()
            self._site = site
            self._sites.setdefault(site, _new_site())

    def record_saved(self, site: str = ""):
        with self._lock:
            data = self._site_data(site)
            if data is not None:
                data["saved"] += 1

    # ── log classification ─────────────────────────────────────────────────

    def feed(self, msg: str):
        if not msg:
            return
        # Handled outside the lock — set_site()/record_saved() lock themselves
        pm = _PHASE_LINE.search(msg)
        if pm:
            self.set_site(pm.group(1))
            return
        if _SAVED_MARK in msg:
            self.record_saved()
            return
        with self._lock:
            data = self._site_data()
            if data is None:
                return

            if "📊" in msg and "summary on" in msg:
                self._reconcile_summary(data, msg)
                return

            m = _KEYWORD_LINE.search(msg)
            if ("▶️" in msg or "▶" in msg) and m:
                self._flush_block()
                self.current_keyword = m.group(1)
                return

            # UNGM has no 📊 summary line; its per-keyword rollup is
            # "... tenders processed for '<kw>'".
            if "tenders processed for" in msg:
                self._flush_block()
                data["keywords_done"] += 1
                return

            reason = self._classify(msg)
            if reason:
                self._block[reason] += 1
                data["rejected"][reason] += 1

    @staticmethod
    def _classify(msg: str) -> str:
        if "🚫" in msg:
            return "negative" if "negative keyword" in msg else "title_miss"
        if "📅" in msg:
            return "stale"
        if "⏭" in msg:
            return "closed"
        if "⏩" in msg:
            return "duplicate"
        if "⚠️" in msg:
            if "No publication date" in msg or "No date in URL" in msg:
                return "no_date"
            if "Error on row" in msg or "Row error" in msg:
                return "error"
        if "❌" in msg and "error" in msg.lower():
            return "error"
        return ""

    def _reconcile_summary(self, data: dict, msg: str):
        m = _SUMMARY_LISTED.search(msg)
        if m:
            data["listed"] += int(m.group(1))
        for reason, rx in _SUMMARY_PHRASES.items():
            pm = rx.search(msg)
            if pm:
                summary_n = int(pm.group(1))
                extra = summary_n - self._block[reason]
                if extra > 0:          # silent skips not logged per-line
                    data["rejected"][reason] += extra
        data["keywords_done"] += 1
        self._flush_block()

    def _flush_block(self):
        self._block = {k: 0 for k in REASON_KEYS}

    def _site_data(self, site: str = "") -> "dict | None":
        key = (site or self._site or "").strip().lower()
        if not key:
            return None
        return self._sites.setdefault(key, _new_site())

    # ── output ─────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """Small live-progress payload for WebSocket streaming."""
        with self._lock:
            totals = self._totals()
            return {
                "current_keyword": self.current_keyword,
                "keywords_done":   totals["keywords_done"],
                "saved":           totals["saved"],
                "rejected":        totals["rejected_total"],
            }

    def to_dict(self) -> dict:
        with self._lock:
            sites = {}
            for name, d in self._sites.items():
                rejected_total = sum(d["rejected"].values())
                sites[name] = {
                    "listed":         max(d["listed"], rejected_total + d["saved"]),
                    "saved":          d["saved"],
                    "keywords_done":  d["keywords_done"],
                    "rejected":       dict(d["rejected"]),
                    "rejected_total": rejected_total,
                }
            return {"sites": sites, "totals": self._totals(sites)}

    def _totals(self, sites: dict = None) -> dict:
        if sites is None:
            sites = {}
            for name, d in self._sites.items():
                rejected_total = sum(d["rejected"].values())
                sites[name] = {
                    "listed":         max(d["listed"], rejected_total + d["saved"]),
                    "saved":          d["saved"],
                    "keywords_done":  d["keywords_done"],
                    "rejected":       d["rejected"],
                    "rejected_total": rejected_total,
                }
        totals = {
            "sites_scanned":  len(sites),
            "listed":         sum(s["listed"] for s in sites.values()),
            "saved":          sum(s["saved"] for s in sites.values()),
            "keywords_done":  sum(s["keywords_done"] for s in sites.values()),
            "rejected_total": sum(s["rejected_total"] for s in sites.values()),
            "rejected":       {k: 0 for k in REASON_KEYS},
        }
        for s in sites.values():
            for k in REASON_KEYS:
                totals["rejected"][k] += s["rejected"].get(k, 0)
        return totals
