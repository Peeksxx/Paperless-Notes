"""Find the link target under a position in one line of Markdown source."""

from __future__ import annotations

from paperless_notes.mdio.lexer import Span, State, Style, lex_line


def link_at(line: str, offset: int, prev_state: int = State.NORMAL) -> str | None:
    """The URL of an inline link, autolink or bare URL covering ``offset``, else None."""
    spans = lex_line(line, prev_state).spans
    for span in spans:
        if not span.start <= offset < span.end:
            continue
        if span.style & Style.URL and not span.style & Style.MARKER:
            return line[span.start : span.end].strip("<>") or None
        if span.style & Style.LINK and not span.style & Style.MARKER:
            return _destination(line, spans, span.end)
    return None


def _destination(line: str, spans: list[Span], after: int) -> str | None:
    for span in spans:
        if span.start >= after and span.style & Style.URL and span.style & Style.MARKER:
            target = line[span.start : span.end].strip()
            if target.startswith("(") and target.endswith(")"):
                target = target[1:-1].strip()
            if target.startswith("<") and ">" in target:
                return target[1 : target.index(">")] or None
            return target.split(" ", 1)[0] or None
    return None
