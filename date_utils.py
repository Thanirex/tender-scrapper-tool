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


def now_ist_naive() -> datetime:
    """IST wall-clock time with tzinfo stripped.

    Use this for every displayed/stored timestamp string so the app always
    shows Indian time, no matter what timezone the host server is set to.
    Stripping tzinfo keeps existing string formats unchanged (no +05:30
    suffix), so old and new DB rows still sort together lexically.
    """
    return datetime.now(tz=IST).replace(tzinfo=None)


def get_max_age_hours(team_id: str | None = "cnk") -> int:
    """Return maximum publication age in hours based on team_id.
    TMI team gets 168 hours (7 days / 1 week).
    CNK team (and default) gets 24 hours (1 day).
    """
    if team_id and str(team_id).lower() == "tmi":
        return 168
    return 24


def is_within_cutoff_ist(date_str: str, max_age_hours: int = 24) -> bool:
    """
    Return True if date_str falls within max_age_hours from now (IST).

    - Full datetime strings: exact window [now - max_age_hours, now].
    - Date-only strings: accepted if cutoff.date() <= date <= today (IST).
    """
    if not date_str:
        return False
    cleaned = date_str.strip()
    now    = now_ist()
    cutoff = now - timedelta(hours=max_age_hours)

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


def is_within_24h_ist(date_str: str) -> bool:
    """Return True if date_str falls within the last 24 hours from now (IST)."""
    return is_within_cutoff_ist(date_str, max_age_hours=24)


def is_deadline_active(date_str: str) -> bool:
    """Return True if date_str represents a deadline that is today or in the future (IST).
    If date_str cannot be parsed, returns True to avoid missing valid tenders.
    """
    if not date_str:
        return True
    cleaned = re.sub(r'^(?:deadline|post deadline|due date|closing date)[:\s]*', '', date_str.strip(), flags=re.IGNORECASE)
    cleaned = re.sub(r'\.', '', cleaned).strip()  # e.g., "07 Aug. 2026" -> "07 Aug 2026"
    now_date = now_ist().date()

    for fmt in _DATE_FORMATS:
        try:
            d = datetime.strptime(cleaned, fmt).date()
            return d >= now_date
        except ValueError:
            continue

    try:
        m = re.search(r'(\d{1,2})\s+([A-Za-z]{3,9})\s+(\d{4})', cleaned)
        if m:
            day, month, year = int(m.group(1)), m.group(2), int(m.group(3))
            for fmt_m in ["%b", "%B"]:
                try:
                    dt = datetime.strptime(f"{day} {month} {year}", f"%d {fmt_m} %Y").date()
                    return dt >= now_date
                except ValueError:
                    pass
    except Exception:
        pass

    return True


def is_date_or_deadline_valid(date_str: str, max_age_hours: int = 24) -> bool:
    """Return True if date_str is either within max_age_hours publication window OR is an active future deadline."""
    if not date_str:
        return False
    if is_within_cutoff_ist(date_str, max_age_hours):
        return True
    if is_deadline_active(date_str):
        return True
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
