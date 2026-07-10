"""Shared keyword-matching helper for all scraper agents.

Keywords must match as whole words, not as substrings — e.g. the keyword
"ILT" must NOT match "Desiltation", but "ILT trainer" or "(ILT)" must match.

Negative keywords (negative_keywords.json) reject a tender even after a
positive keyword matched.  "title_only" entries reject only on the tender
title; "anywhere" entries reject on the title or the detail-page text.
"""
import json
import re
from pathlib import Path


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


# ── Negative keywords ────────────────────────────────────────────────────────

_NEG_FILE = Path(__file__).parent / "negative_keywords.json"
_neg_cache = {"mtime": None, "title_only": [], "anywhere": []}


def _compile_list(keywords) -> list:
    compiled = []
    for kw in keywords or []:
        if not isinstance(kw, str):
            continue
        pattern = _build_pattern(kw)
        if pattern is not None:
            compiled.append((kw.strip(), pattern))
    return compiled


def _load_negative_keywords() -> dict:
    """Load negative_keywords.json, recompiling only when the file changes.

    Accepts {"title_only": [...], "anywhere": [...]} or a plain list
    (treated as title_only).  A missing or unparsable file disables the
    filter rather than blocking scrapes.
    """
    try:
        mtime = _NEG_FILE.stat().st_mtime
    except OSError:
        return {"title_only": [], "anywhere": []}

    if _neg_cache["mtime"] != mtime:
        try:
            with open(_NEG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        if isinstance(data, list):
            data = {"title_only": data}
        _neg_cache["title_only"] = _compile_list(data.get("title_only"))
        _neg_cache["anywhere"]   = _compile_list(data.get("anywhere"))
        _neg_cache["mtime"] = mtime
    return _neg_cache


def find_negative_keyword(title: str, *detail_texts: str) -> "str | None":
    """Return the negative keyword that disqualifies this tender, else None.

    The title is checked against both scopes; detail texts (description,
    full page text) only against the "anywhere" scope, so generic
    title-only words can't be tripped by nav menus or boilerplate.
    """
    neg = _load_negative_keywords()
    if title:
        for kw, pattern in neg["title_only"]:
            if pattern.search(title):
                return kw
    texts = [t for t in (title, *detail_texts) if t]
    for kw, pattern in neg["anywhere"]:
        if any(pattern.search(t) for t in texts):
            return kw
    return None
