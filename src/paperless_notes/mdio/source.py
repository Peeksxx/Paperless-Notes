"""Where the caret is, in Markdown terms: positions, lexer state and code or link context of a line.

Editing commands derive their ranges here. Lexer state comes from the highlighter's block states; a note
without a highlighter (above the size limit) is lexed from a bounded number of lines back, so no command
ever scans the whole document.
"""

from __future__ import annotations

from PySide6.QtGui import QTextBlock, QTextCursor, QTextDocument

from paperless_notes.mdio.highlighter import lexer_state
from paperless_notes.mdio.lexer import LineResult, State, Style, lex_line

LOOKBACK_LINES = 400


def units(text: str) -> int:
    """Length of ``text`` in UTF-16 code units, the unit of document positions."""
    return len(text.encode("utf-16-le")) // 2


def position_of(block: QTextBlock, index: int) -> int:
    """Document position of the character at Python index ``index`` in ``block``."""
    text = block.text()
    if text.isascii():
        return block.position() + index
    return block.position() + units(text[:index])


def index_of(block: QTextBlock, position: int) -> int:
    """Python index in ``block.text()`` of document position ``position`` (clamped to the line)."""
    text = block.text()
    offset = max(0, position - block.position())
    if text.isascii():
        return min(offset, len(text))
    count = 0
    for i, ch in enumerate(text):
        if count >= offset:
            return i
        count += 2 if ord(ch) > 0xFFFF else 1
    return len(text)


def selected_blocks(cursor: QTextCursor) -> list[QTextBlock]:
    """Blocks touched by the selection; a selection ending at a line start excludes that line."""
    doc = cursor.document()
    first = doc.findBlock(cursor.selectionStart())
    last = doc.findBlock(cursor.selectionEnd())
    if cursor.hasSelection() and last.position() == cursor.selectionEnd() and last != first:
        last = last.previous()
    blocks = [first]
    while blocks[-1] != last and blocks[-1].next().isValid():
        blocks.append(blocks[-1].next())
    return blocks


def state_before(block: QTextBlock) -> int:
    """Lexer state at the start of ``block``."""
    previous = block.previous()
    if not previous.isValid():
        return State.NORMAL
    known = lexer_state(previous.userState())
    if known >= 0:
        return known
    start = previous
    steps = 0
    while start.previous().isValid() and steps < LOOKBACK_LINES:
        start = start.previous()
        steps += 1
    state = State.NORMAL
    current = start
    while current.isValid() and current != block:
        following = current.next()
        next_text = following.text() if following.isValid() else None
        state = lex_line(current.text(), state, current.blockNumber() == 0, next_text).state
        current = following
    return state


def line_result(block: QTextBlock) -> LineResult:
    following = block.next()
    next_text = following.text() if following.isValid() else None
    return lex_line(block.text(), state_before(block), block.blockNumber() == 0, next_text)


def in_fence(block: QTextBlock) -> bool:
    """True for lines inside a fenced code block and for its fence lines."""
    if State.fence_info(state_before(block)) is not None:
        return True
    return State.fence_info(line_result(block).state) is not None


def in_raw_region(block: QTextBlock) -> bool:
    """Fenced code, front matter or an HTML comment: Markdown commands do not apply there."""
    before = state_before(block)
    if before in (State.FRONT_MATTER, State.HTML_COMMENT) or in_fence(block):
        return True
    return line_result(block).state in (State.FRONT_MATTER, State.HTML_COMMENT)


def style_at(block: QTextBlock, index: int) -> Style:
    """Union of lexer styles covering the character at ``index`` (or just before the line end)."""
    style = Style.NONE
    for span in line_result(block).spans:
        if span.start <= index < span.end:
            style |= span.style
    return style


def is_literal(block: QTextBlock, index: int) -> bool:
    """Inside inline code, a link destination, an autolink or a bare URL, where text is not Markdown."""
    style = style_at(block, index)
    return bool(style & (Style.CODE | Style.CODE_BLOCK | Style.URL | Style.HTML | Style.META))


def block_range(document: QTextDocument, first: QTextBlock, last: QTextBlock) -> QTextCursor:
    """A cursor selecting the text of ``first`` through ``last`` without the final line break."""
    cursor = QTextCursor(document)
    cursor.setPosition(first.position())
    cursor.setPosition(last.position() + last.length() - 1, QTextCursor.MoveMode.KeepAnchor)
    return cursor
