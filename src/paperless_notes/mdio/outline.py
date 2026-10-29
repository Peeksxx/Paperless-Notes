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
from paperless_notes.mdio.lexer import (
    MAX_PARAGRAPH_LINES,
    LineResult,
    State,
    Style,
    continues,
    lex_line,
    paragraph_role,
)

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


def _setext_paragraph(
    lines: list[str], number: int, result: LineResult, before: list[int]
) -> tuple[int, str] | None:
    """For a heading underlined with ``---`` or ``===`` at ``number``: the first line and the words of the
    whole paragraph above the underline, or None when that paragraph belongs to a list item or quote."""
    role = paragraph_role(lines[number], result)
    if role is None:
        return number, heading_text(lines[number], result)
    words = [heading_text(lines[number], result)]
    first = number
    current = role
    while not current.item and first > 0 and number - first < MAX_PARAGRAPH_LINES:
        above = first - 1
        state = before[above] if before[above] >= 0 else State.NORMAL
        found = lex_line(lines[above], state, above == 0, lines[first])
        found_role = paragraph_role(lines[above], found)
        if found_role is None or found.heading or not continues(found_role, current):
            break
        words.insert(0, heading_text(lines[above], found))
        first, current = above, found_role
    if current.item or current.quoted:
        return None
    return first, " ".join(word for word in words if word)


def outline_of_lines(lines: list[str], states: list[int] | None = None) -> Outline:
    """Headings of ``lines``. ``states[n]``, when given and not negative, is the lexer state after line n."""
    headings: list[Heading] = []
    state = State.NORMAL
    before = [-1] * len(lines)
    for number, line in enumerate(lines):
        known = states[number - 1] if states is not None and number > 0 else -1
        if states is not None and known >= 0:
            state = known
        before[number] = state
        if states is not None and known >= 0 and not _candidate(lines, number):
            continue
        following = lines[number + 1] if number + 1 < len(lines) else None
        result = lex_line(line, state, number == 0, following)
        state = result.state
        if result.heading:
            if len(headings) >= MAX_HEADINGS:
                return Outline(tuple(headings), truncated=True)
            if _ATX_START.match(line):
                headings.append(Heading(result.heading, heading_text(line, result), number))
                continue
            found = _setext_paragraph(lines, number, result, before)
            if found is not None:
                headings.append(Heading(result.heading, found[1], found[0]))
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
