"""Shared keyword-matching helper for all scraper agents.

Keywords must match as whole words, not as substrings — e.g. the keyword
"ILT" must NOT match "Desiltation", but "ILT trainer" or "(ILT)" must match.
"""
import re


def _build_pattern(keyword: str) -> "re.Pattern | None":
    kw = (keyword or "").strip()
    if not kw:
        return None
    # Escape the keyword, then let any run of whitespace in a multi-word
    # keyword match flexible whitespace/hyphens in the text.
    escaped = re.escape(kw)
    escaped = re.sub(r"(\\\s|\s)+", r"[\\s\\-]+", escaped)
    # (?<!\w) / (?!\w) instead of \b so keywords that start or end with a
    # non-word character (e.g. ".NET") still anchor correctly.
    return re.compile(r"(?<!\w)" + escaped + r"(?!\w)", re.IGNORECASE)


def keyword_matches(keyword: str, *texts: str) -> bool:
    """True if the keyword appears as a whole word/phrase in any given text."""
    pattern = _build_pattern(keyword)
    if pattern is None:
        return False
    return any(pattern.search(t) for t in texts if t)
