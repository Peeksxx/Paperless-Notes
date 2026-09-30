"""What the user typed into note search, compiled into a safe full-text expression.

User text is data, never FTS5 syntax: every word or quoted phrase becomes one quoted FTS5 string (inner
quotes doubled), so operators such as ``OR``, ``NEAR``, ``*``, ``^``, column filters and parentheses are
matched as ordinary text. Words are combined with AND; the last word also matches as a prefix while it is
being typed. ``#tag`` words filter by tag instead of matching text.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from paperless_notes.core.tagnames import tag_key, valid_tag_name

MAX_QUERY_CHARS = 200
MAX_TERMS = 12


@dataclass(frozen=True, slots=True)
class CompiledQuery:
    """``match`` is the FTS5 expression ('' for a tag-only query); ``terms`` are the words and phrases as
    typed, for revealing a match in the note; ``tags`` are tag keys (casefolded, without ``#``)."""

    match: str
    terms: tuple[str, ...]
    tags: tuple[str, ...]
    truncated: bool = False


def _has_word_character(text: str) -> bool:
    return any(ch.isalnum() for ch in text)


def _quoted(text: str) -> str:
    return '"' + text.replace('"', '""') + '"'


def _tokens(text: str) -> list[tuple[str, bool]]:
    """(token, quoted) pairs; an unclosed quote runs to the end of the text."""
    tokens: list[tuple[str, bool]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        if text[i] == '"':
            end = text.find('"', i + 1)
            end = n if end < 0 else end
            tokens.append((text[i + 1 : end], True))
            i = end + 1
            continue
        start = i
        while i < n and not text[i].isspace() and text[i] != '"':
            i += 1
        tokens.append((text[start:i], False))
    return tokens


def compile_query(text: str) -> CompiledQuery | None:
    """None when the text contains nothing searchable."""
    truncated = len(text) > MAX_QUERY_CHARS
    text = text[:MAX_QUERY_CHARS]
    # SQLite strings cannot contain NUL, and invisible format controls have no useful search meaning.
    # Treat them as token boundaries instead of passing them to FTS5 or joining words around them.
    text = "".join(" " if unicodedata.category(ch).startswith("C") else ch for ch in text)
    parts: list[str] = []
    terms: list[str] = []
    tags: list[str] = []
    tokens = _tokens(text)
    for index, (token, quoted) in enumerate(tokens):
        if len(terms) + len(tags) >= MAX_TERMS:
            truncated = True
            break
        if not quoted and token.startswith("#") and valid_tag_name(token[1:]):
            key = tag_key(token[1:])
            if key not in tags:
                tags.append(key)
            continue
        phrase = " ".join(token.split())
        if not _has_word_character(phrase):
            continue
        last = index == len(tokens) - 1 and not quoted and not text[-1:].isspace()
        parts.append(_quoted(phrase) + ("*" if last else ""))
        terms.append(phrase)
    if not parts and not tags:
        return None
    return CompiledQuery(" ".join(parts), tuple(terms), tuple(tags), truncated)
