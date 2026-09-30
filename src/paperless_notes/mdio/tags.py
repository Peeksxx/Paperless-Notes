"""Tags in note text, found through the Markdown lexer (grammar in ``core.tagnames``).

Tag-looking text in fenced or inline code, URLs, link destinations, raw HTML, HTML comments and front
matter is never a tag, and neither is a heading marker, because the ``#`` must sit in ordinary text.
"""

from __future__ import annotations

from paperless_notes.core.security.limits import MAX_INLINE_CHARS
from paperless_notes.core.tagnames import MAX_TAGS_PER_NOTE, TAG_PATTERN, Tag, acceptable, tag_key
from paperless_notes.mdio.lexer import Span, State, Style, inline_spans, lex_line

_EXCLUDED = Style.CODE | Style.CODE_BLOCK | Style.URL | Style.HTML | Style.META | Style.MARKER | Style.ESCAPE


def _excluded(spans: list[Span], position: int) -> bool:
    return any(s.start <= position < s.end and s.style & _EXCLUDED for s in spans)


def tags_in_line(line: str, spans: list[Span]) -> list[tuple[int, int, str]]:
    """``(start, end, name)`` of every tag in one lexed line; ``start`` is the index of the ``#``."""
    if "#" not in line or len(line) > MAX_INLINE_CHARS:
        return []
    if any(s.style & Style.TABLE for s in spans):
        spans = [*spans, *inline_spans(line)]
    found: list[tuple[int, int, str]] = []
    for match in TAG_PATTERN.finditer(line):
        name = match.group(1)
        if acceptable(name) and not _excluded(spans, match.start()):
            found.append((match.start(), match.end(), name))
    return found


def extract_tags(text: str, limit: int = MAX_TAGS_PER_NOTE) -> list[Tag]:
    """Distinct tags of a note in first-seen order, at most ``limit``. Linear in the text length."""
    if "#" not in text:
        return []
    lines = text.split("\n")
    state = State.NORMAL
    seen: dict[str, Tag] = {}
    for number, line in enumerate(lines):
        following = lines[number + 1] if number + 1 < len(lines) else None
        result = lex_line(line, state, number == 0, following)
        state = result.state
        for _start, _end, name in tags_in_line(line, result.spans):
            key = tag_key(name)
            if key not in seen:
                seen[key] = Tag(key, name)
                if len(seen) >= limit:
                    return list(seen.values())
    return list(seen.values())
