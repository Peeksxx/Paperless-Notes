"""Find and replace over a note document, in document positions (UTF-16 code units).

Matching runs in Qt's PCRE2 engine on the document's own text, so every range is a real document position
even around characters outside the Basic Multilingual Plane; nothing is lower-cased in Python and no
Python string index is ever used as a position. Matches never span a line break. Work is bounded: the
pattern length, the number of matches and the time spent are limited, and PCRE2's own match limit stops
runaway backtracking inside one line.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from PySide6.QtCore import QRegularExpression, QRegularExpressionMatch
from PySide6.QtGui import QTextCursor, QTextDocument

MAX_PATTERN_CHARS = 500
MAX_MATCHES = 10_000
TIME_BUDGET_S = 0.2
_BLOCK_SEPARATOR = "\N{PARAGRAPH SEPARATOR}"
_TEMPLATE = re.compile(r"\$(\$|\d{1,2})")


@dataclass(frozen=True, slots=True)
class FindOptions:
    text: str = ""
    case_sensitive: bool = False
    whole_word: bool = False
    regex: bool = False


@dataclass(frozen=True, slots=True)
class FindResult:
    """Match ranges ``(start, end)`` in document positions, in order. ``complete`` is False when the
    count or time limit stopped the search early."""

    ranges: tuple[tuple[int, int], ...] = ()
    complete: bool = True


class Finder:
    """A compiled search. ``problem`` explains, in plain words, why a pattern cannot be used."""

    def __init__(self, options: FindOptions) -> None:
        self.options = options
        self.problem = ""
        self.expression: QRegularExpression | None = None
        text = options.text
        if not text:
            return
        if len(text) > MAX_PATTERN_CHARS:
            self.problem = f"The search text is too long ({MAX_PATTERN_CHARS} characters at most)."
            return
        source = text if options.regex else QRegularExpression.escape(text)
        if options.whole_word:
            source = f"(?<!\\w)(?:{source})(?!\\w)"
        flags = (
            QRegularExpression.PatternOption.UseUnicodePropertiesOption
            | QRegularExpression.PatternOption.MultilineOption
        )
        if not options.case_sensitive:
            flags |= QRegularExpression.PatternOption.CaseInsensitiveOption
        expression = QRegularExpression(source, flags)
        if not expression.isValid():
            self.problem = f"This pattern is not valid: {expression.errorString()}."
            return
        if expression.match("").hasMatch():
            self.problem = "This pattern also matches empty text; add something it must find."
            return
        self.expression = expression

    @property
    def ready(self) -> bool:
        return self.expression is not None

    def find_all(
        self,
        document: QTextDocument,
        max_matches: int = MAX_MATCHES,
        budget_s: float = TIME_BUDGET_S,
        clock: Callable[[], float] = time.monotonic,
    ) -> FindResult:
        found: list[tuple[int, int]] = []
        complete = True
        for match in self.iter_matches(document, max_matches, budget_s, clock):
            if match is None:
                complete = False
                break
            start = match.capturedStart()
            found.append((start, start + match.capturedLength()))
        return FindResult(tuple(found), complete)

    def iter_matches(
        self,
        document: QTextDocument,
        max_matches: int,
        budget_s: float,
        clock: Callable[[], float],
    ) -> Iterator[QRegularExpressionMatch | None]:
        """Non-empty single-line matches in order; a final None means a limit stopped the search."""
        if self.expression is None:
            return
        deadline = clock() + budget_s
        count = 0
        steps = 0
        matches = self.expression.globalMatch(document_text(document))
        while matches.hasNext():
            match = matches.next()
            steps += 1
            if match.capturedLength() and "\n" not in match.captured(0):
                if count >= max_matches:
                    yield None
                    return
                count += 1
                yield match
            if steps % 64 == 0 and clock() > deadline:
                yield None
                return

    def match_at(self, document: QTextDocument, start: int, end: int) -> QRegularExpressionMatch | None:
        """The match that still covers exactly ``start``..``end``, or None when the text changed."""
        if self.expression is None:
            return None
        block = document.findBlock(start)
        if not block.isValid() or end > block.position() + block.length() - 1:
            return None
        offset = start - block.position()
        match = self.expression.match(block.text(), offset)
        if (
            match.hasMatch()
            and match.capturedStart() == offset
            and match.capturedEnd() == end - block.position()
        ):
            return match
        return None

    def replacement(self, match: QRegularExpressionMatch, template: str) -> str:
        """The replacement text: literal, or in regex mode with ``$1`` to ``$99`` and ``$$`` expanded."""
        if not self.options.regex:
            return template

        def group(found: re.Match[str]) -> str:
            reference = found.group(1)
            if reference == "$":
                return "$"
            number = int(reference)
            return match.captured(number) if number <= match.lastCapturedIndex() else ""

        return _TEMPLATE.sub(group, template)


def document_text(document: QTextDocument) -> str:
    """The document's text with one line feed between lines, character for character with positions."""
    return document.toRawText().replace(_BLOCK_SEPARATOR, "\n")


def replace_range(document: QTextDocument, finder: Finder, start: int, end: int, template: str) -> int | None:
    """Replace one match as one undo step and return the end of the new text; None (nothing changed)
    when the text there no longer matches."""
    match = finder.match_at(document, start, end)
    if match is None:
        return None
    cursor = QTextCursor(document)
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    cursor.beginEditBlock()
    cursor.insertText(finder.replacement(match, template))
    cursor.endEditBlock()
    return cursor.position()


def replace_all(
    document: QTextDocument,
    finder: Finder,
    template: str,
    max_matches: int = MAX_MATCHES,
    budget_s: float = TIME_BUDGET_S,
) -> int | None:
    """Replace every match as one undo step. None (nothing changed) when a limit stopped the search."""
    planned: list[tuple[int, int, str]] = []
    for match in finder.iter_matches(document, max_matches, budget_s, time.monotonic):
        if match is None:
            return None
        start = match.capturedStart()
        planned.append((start, start + match.capturedLength(), finder.replacement(match, template)))
    if not planned:
        return 0
    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    for start, end, text in reversed(planned):
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        cursor.insertText(text)
    cursor.endEditBlock()
    return len(planned)
