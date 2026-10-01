"""Emoji shortcodes such as ``:skull:``: the names (GitHub's gemoji list), the ``:query`` being typed at the
caret, the best matches for it, and a completed ``:name:`` to replace.

A shortcode starts at a colon that begins a word (line start, a space or an opening bracket before it), so
times such as 10:30 and addresses such as https:// never open it.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

EMOJI_FILE = Path(__file__).resolve().parents[1] / "data" / "emoji.tsv"
MIN_QUERY = 2
MAX_MATCHES = 8
_NAME = re.compile(r"[a-z0-9_+\-]+")
_OPENERS = " \t([{\"'"


@lru_cache(maxsize=1)
def emoji_names(path: Path = EMOJI_FILE) -> tuple[tuple[str, str], ...]:
    """(name, emoji) pairs in the file's order."""
    pairs: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name, _, emoji = line.partition("\t")
        if name and emoji:
            pairs.append((name, emoji))
    return tuple(pairs)


@lru_cache(maxsize=1)
def emoji_by_name(path: Path = EMOJI_FILE) -> dict[str, str]:
    return dict(emoji_names(path))


def query_at(text: str, column: int) -> tuple[int, str] | None:
    """(colon position, query) when the caret at ``column`` ends a shortcode being typed."""
    start = text.rfind(":", 0, column)
    if start < 0:
        return None
    query = text[start + 1 : column]
    if len(query) < MIN_QUERY or not _NAME.fullmatch(query):
        return None
    if start > 0 and text[start - 1] not in _OPENERS:
        return None
    return start, query


def matches(query: str, limit: int = MAX_MATCHES, path: Path = EMOJI_FILE) -> list[tuple[str, str]]:
    """Names starting with ``query`` (shortest first), then names containing it."""
    lowered = query.lower()
    names = emoji_names(path)
    starts = sorted((pair for pair in names if pair[0].startswith(lowered)), key=lambda p: (len(p[0]), p[0]))
    inside = [pair for pair in names if lowered in pair[0] and not pair[0].startswith(lowered)]
    return (starts + inside)[:limit]


def completed_at(text: str, column: int, path: Path = EMOJI_FILE) -> tuple[int, str] | None:
    """(colon position, emoji) when the caret at ``column`` has just closed a known ``:name:``."""
    if column < 2 or text[column - 1] != ":":
        return None
    found = query_at(text, column - 1)
    if found is None:
        return None
    start, name = found
    emoji = emoji_by_name(path).get(name)
    return (start, emoji) if emoji is not None else None
