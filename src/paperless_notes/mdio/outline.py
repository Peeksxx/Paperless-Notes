"""The headings of a note, as the Markdown lexer sees them.

Only lines the lexer classifies as ATX or setext headings count, so ``#`` lines inside fenced code, front
matter or HTML comments are never headings. With a live highlighter the lexer state of each line comes
from the block states it keeps, so only candidate lines are lexed; otherwise the note is lexed from the
top. Very large notes get a "too large" outline instead of a long pause.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from PySide6.QtGui import QTextDocument

from paperless_notes.mdio.highlighter import lexer_state
from paperless_notes.mdio.lexer import LineResult, State, Style, lex_line

MAX_OUTLINE_CHARS = 1_000_000
MAX_HEADINGS = 5_000
_ATX_START = re.compile(r" {0,3}#{1,6}(?:[ \t]|$)")
_SETEXT_LINE = re.compile(r" {0,3}(?:=+|-+)[ \t]*$")


@dataclass(frozen=True, slots=True)
class Heading:
    level: int
    text: str
    line: int


@dataclass(frozen=True, slots=True)
class Outline:
    headings: tuple[Heading, ...] = ()
    too_large: bool = False
    truncated: bool = False


def heading_text(line: str, result: LineResult) -> str:
    """The words of a heading: markers (``#``, emphasis, code ticks, link syntax) removed."""
    hidden = bytearray(len(line))
    for span in result.spans:
        if span.style & Style.MARKER:
            hidden[max(0, span.start) : max(0, span.end)] = b"\x01" * (
                min(len(line), span.end) - max(0, span.start)
            )
    kept = "".join(ch for ch, flag in zip(line, hidden, strict=False) if not flag)
    return " ".join(kept.split())


def _candidate(lines: list[str], number: int) -> bool:
    line = lines[number]
    if _ATX_START.match(line):
        return True
    following = lines[number + 1] if number + 1 < len(lines) else None
    return following is not None and line.strip() != "" and _SETEXT_LINE.match(following) is not None


def outline_of_lines(lines: list[str], states: list[int] | None = None) -> Outline:
    """Headings of ``lines``. ``states[n]``, when given and not negative, is the lexer state after line n."""
    headings: list[Heading] = []
    state = State.NORMAL
    for number, line in enumerate(lines):
        known = states[number - 1] if states is not None and number > 0 else -1
        if states is not None and known >= 0:
            state = known
            if not _candidate(lines, number):
                continue
        following = lines[number + 1] if number + 1 < len(lines) else None
        result = lex_line(line, state, number == 0, following)
        state = result.state
        if result.heading:
            if len(headings) >= MAX_HEADINGS:
                return Outline(tuple(headings), truncated=True)
            headings.append(Heading(result.heading, heading_text(line, result), number))
    return Outline(tuple(headings))


def outline_of_document(document: QTextDocument, max_chars: int = MAX_OUTLINE_CHARS) -> Outline:
    if document.characterCount() > max_chars:
        return Outline(too_large=True)
    lines = document.toRawText().split("\N{PARAGRAPH SEPARATOR}")
    if len(lines) > document.blockCount():
        lines = lines[: document.blockCount()]
    states: list[int] | None = None
    first = document.firstBlock()
    if first.isValid() and lexer_state(first.userState()) >= 0:
        states = []
        block = first
        while block.isValid():
            states.append(lexer_state(block.userState()))
            block = block.next()
    return outline_of_lines(lines, states)
