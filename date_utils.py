import re
from datetime import datetime, timezone, timedelta

IST = timezone(timedelta(hours=5, minutes=30))

# Ordered from most-specific to least-specific.
# All are tried with the raw date string before giving up.
_DATE_FORMATS = [
    "%d %B %Y",    # 07 May 2026
    "%d %b %Y",    # 07 May 2026
    "%Y-%m-%d",    # 2026-05-07
    "%d/%m/%Y",    # 07/05/2026
    "%d-%m-%Y",    # 07-05-2026
    "%d.%m.%Y",    # 07.05.2026
    "%B %d, %Y",   # May 07, 2026
    "%b %d, %Y",   # May 07, 2026
    "%d %B, %Y",   # 07 May, 2026
    "%d-%b-%Y",    # 07-May-2026
    "%m/%d/%Y",    # 05/07/2026 (US — lowest priority, ambiguous)
]


def now_ist() -> datetime:
    return datetime.now(tz=IST)


def is_within_24h_ist(date_str: str) -> bool:
    """
    Return True if date_str falls within the last 24 hours from now (IST).

    - Full datetime strings: exact window [now-24h, now].
    - Date-only strings: accepted if cutoff.date() <= date <= today (IST).
      Practically: today OR yesterday, depending on when the run executes.
    """
    if not date_str:
        return False
    cleaned = date_str.strip()
    now    = now_ist()
    cutoff = now - timedelta(hours=24)

    # Try fromisoformat first — handles microseconds, +HH:MM offsets, etc.
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        dt_ist = dt.astimezone(IST)
        return cutoff <= dt_ist <= now
    except ValueError:
        pass

    # Explicit strptime formats (no-TZ UTC variant)
    try:
        dt = datetime.strptime(cleaned, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return cutoff <= dt.astimezone(IST) <= now
    except ValueError:
        pass

    # Plain date formats — compare date portion only, reject future dates
    today       = now.date()
    cutoff_date = cutoff.date()
    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(cleaned, fmt).date()
            return cutoff_date <= d <= today
        except ValueError:
            continue

    return False


# Keep old name as an alias so any external callers don't break
is_today_ist = is_within_24h_ist


# Patterns to pull a publication date out of free-form page text.
# Ordered: labelled patterns first (higher confidence), bare dates last.
_PUB_PATTERNS = [
    # "Published on: 07 May 2026",  "Posted: 2026-05-07", "Date Posted: ..."
    (
        r'(?:published|posted|date\s+posted|publication\s+date)\s*(?:on\s*)?'
        r'[:\-–]?\s*'
        r'(\d{1,2}[\s\-/\.]\w+[\s\-/\.]\d{2,4}'
        r'|\w+\s+\d{1,2},?\s+\d{4}'
        r'|\d{4}[\-/]\d{2}[\-/]\d{2})'
    ),
    # "07 May 2026"  or  "7 May 2026"
    r'\b(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})\b',
    # "07-May-2026"
    r'\b(\d{1,2}-(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*-\d{4})\b',
    # "2026-05-07"
    r'\b(\d{4}-\d{2}-\d{2})\b',
    # "07/05/2026"  or  "07-05-2026"
    r'\b(\d{1,2}[/-]\d{1,2}[/-]\d{4})\b',
]


def extract_date_from_text(text: str) -> str | None:
    """
    Search free-form page text for a publication date.
    Returns the raw matched date string, or None if nothing found.
    """
    for pat in _PUB_PATTERNS:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return None


def normalize_title(title: str) -> str:
    """Lowercase + strip punctuation for deduplication comparison."""
    t = title.lower()
    t = re.sub(r'[^a-z0-9\s]', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t
