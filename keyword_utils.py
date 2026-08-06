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


def _build_patterns(keyword: str) -> list:
    kw = (keyword or "").strip()
    if not kw:
        return []

    candidates = [kw]
    # If keyword has parenthetical abbreviation e.g. "Learning Management System (LMS)"
    paren_match = re.search(r"^(.+?)\s*\((.+?)\)$", kw)
    if paren_match:
        full_part = paren_match.group(1).strip()
        abbr_part = paren_match.group(2).strip()
        if full_part:
            candidates.append(full_part)
        if abbr_part:
            candidates.append(abbr_part)

    # If keyword has slash e.g. "eLearning Platform / Digital Learning Solution"
    if "/" in kw:
        parts = [p.strip() for p in re.split(r"\s*/\s*", kw) if p.strip()]
        candidates.extend(parts)

    patterns = []
    seen = set()
    for cand in candidates:
        if cand.lower() in seen:
            continue
        seen.add(cand.lower())
        escaped = re.escape(cand)
        escaped = re.sub(r"(\\\s|\s)+", r"[\\s\\-]+", escaped)
        p = re.compile(r"(?<!\w)" + escaped + r"(?!\w)", re.IGNORECASE)
        patterns.append(p)

    return patterns


def keyword_matches(keyword: str, *texts: str) -> bool:
    """True if the keyword (or any of its primary sub-phrases) appears as a whole word/phrase in any given text."""
    patterns = _build_patterns(keyword)
    if not patterns:
        return False
    return any(p.search(t) for p in patterns for t in texts if t)


# ── Negative keywords ────────────────────────────────────────────────────────

_NEG_FILE = Path(__file__).parent / "negative_keywords.json"
_neg_cache = {}


def _compile_list(keywords) -> list:
    compiled = []
    for kw in keywords or []:
        if not isinstance(kw, str):
            continue
        patterns = _build_patterns(kw)
        for pattern in patterns:
            compiled.append((kw.strip(), pattern))
    return compiled


def _load_negative_keywords(team_id: str = "cnk") -> dict:
    """Load team-specific negative_keywords.json, recompiling when the file changes.

    Accepts {"title_only": [...], "anywhere": [...]} or a plain list.
    Falls back to root negative_keywords.json if team file doesn't exist.
    """
    tid = (team_id or "cnk").strip().lower()
    team_neg_file = Path(__file__).parent / "configs" / "teams" / tid / "negative_keywords.json"
    target_file = team_neg_file if team_neg_file.exists() else _NEG_FILE

    try:
        mtime = target_file.stat().st_mtime
    except OSError:
        return {"title_only": [], "anywhere": []}

    cache_entry = _neg_cache.get(tid, {})
    if cache_entry.get("mtime") != mtime:
        try:
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            data = {}
        if isinstance(data, list):
            data = {"title_only": data}
        cache_entry = {
            "title_only": _compile_list(data.get("title_only")),
            "anywhere":   _compile_list(data.get("anywhere")),
            "mtime":      mtime,
        }
        _neg_cache[tid] = cache_entry

    return cache_entry


def find_negative_keyword(title: str, *detail_texts: str, team_id: str = "cnk") -> "str | None":
    """Return the negative keyword that disqualifies this tender, else None.

    The title is checked against both scopes; detail texts (description,
    full page text) only against the "anywhere" scope.
    """
    neg = _load_negative_keywords(team_id)
    if title:
        for kw, pattern in neg.get("title_only", []):
            if pattern.search(title):
                return kw
    texts = [t for t in (title, *detail_texts) if t]
    for kw, pattern in neg.get("anywhere", []):
        if any(pattern.search(t) for t in texts):
            return kw
    return None
