"""Formatting commands for Markdown source. Each is one undo step and edits only the lines it targets."""

from __future__ import annotations

import re
from enum import Enum

from PySide6.QtGui import QTextBlock, QTextCursor

_HEADING = re.compile(r"( {0,3})(#{1,6})([ \t]+|$)")
_LIST = re.compile(r"( *)([-+*]|\d{1,9}[.)])[ \t]+(\[[ xX]\][ \t]+)?")
_QUOTE = re.compile(r"( {0,3})>[ ]?")


class ListKind(Enum):
    BULLET = "bullet"
    ORDERED = "ordered"
    TASK = "task"


def _blocks(cursor: QTextCursor) -> list[QTextBlock]:
    doc = cursor.document()
    first = doc.findBlock(cursor.selectionStart())
    last = doc.findBlock(cursor.selectionEnd())
    if cursor.hasSelection() and last.position() == cursor.selectionEnd() and last != first:
        last = last.previous()
    blocks = [first]
    while blocks[-1] != last and blocks[-1].next().isValid():
        blocks.append(blocks[-1].next())
    return blocks


def _replace_block_prefix(block: QTextBlock, old_len: int, new_prefix: str) -> None:
    if block.text()[:old_len] == new_prefix:
        return
    c = QTextCursor(block)
    c.setPosition(block.position())
    c.setPosition(block.position() + old_len, QTextCursor.MoveMode.KeepAnchor)
    c.insertText(new_prefix)


def toggle_heading(cursor: QTextCursor, level: int) -> None:
    blocks = _blocks(cursor)
    cursor.beginEditBlock()
    try:
        all_same = all(
            (m := _HEADING.match(b.text())) is not None and len(m.group(2)) == level for b in blocks
        )
        for block in blocks:
            text = block.text()
            m = _HEADING.match(text)
            old_len = m.end() if m else 0
            _replace_block_prefix(block, old_len, "" if all_same else "#" * level + " ")
    finally:
        cursor.endEditBlock()


def toggle_list(cursor: QTextCursor, kind: ListKind) -> None:
    blocks = _blocks(cursor)
    cursor.beginEditBlock()
    try:

        def is_kind(text: str) -> bool:
            m = _LIST.match(text)
            if m is None:
                return False
            ordered = m.group(2)[0].isdigit()
            task = m.group(3) is not None
            return {
                ListKind.BULLET: not ordered and not task,
                ListKind.ORDERED: ordered,
                ListKind.TASK: task,
            }[kind]

        remove = all(is_kind(b.text()) for b in blocks)
        number = 1
        for block in blocks:
            text = block.text()
            m = _LIST.match(text)
            indent = m.group(1) if m else ""
            old_len = m.end() if m else 0
            if remove:
                _replace_block_prefix(block, old_len, indent)
                continue
            if kind is ListKind.ORDERED:
                marker = f"{number}. "
                number += 1
            elif kind is ListKind.TASK:
                marker = "- [ ] "
            else:
                marker = "- "
            _replace_block_prefix(block, old_len, indent + marker)
    finally:
        cursor.endEditBlock()


def toggle_quote(cursor: QTextCursor) -> None:
    blocks = _blocks(cursor)
    cursor.beginEditBlock()
    try:
        remove = all(_QUOTE.match(b.text()) for b in blocks)
        for block in blocks:
            m = _QUOTE.match(block.text())
            if remove and m:
                _replace_block_prefix(block, m.end(), m.group(1))
            elif not remove:
                _replace_block_prefix(block, 0, "> ")
    finally:
        cursor.endEditBlock()


def toggle_wrap(cursor: QTextCursor, opening: str, closing: str | None = None) -> None:
    """Bold (``**``), italic (``*``), strikethrough (``~~``), code (`` ` ``) or ``<u>``/``</u>``."""
    closing = opening if closing is None else closing
    doc = cursor.document()
    start, end = cursor.selectionStart(), cursor.selectionEnd()
    cursor.beginEditBlock()
    try:
        before = QTextCursor(doc)
        before.setPosition(max(0, start - len(opening)))
        before.setPosition(start, QTextCursor.MoveMode.KeepAnchor)
        after = QTextCursor(doc)
        after.setPosition(end)
        after.setPosition(min(doc.characterCount() - 1, end + len(closing)), QTextCursor.MoveMode.KeepAnchor)
        if start != end and before.selectedText() == opening and after.selectedText() == closing:
            after.removeSelectedText()
            before.removeSelectedText()
            return
        insert_after = QTextCursor(doc)
        insert_after.setPosition(end)
        insert_after.insertText(closing)
        insert_before = QTextCursor(doc)
        insert_before.setPosition(start)
        insert_before.insertText(opening)
        if start == end:
            cursor.setPosition(start + len(opening))
    finally:
        cursor.endEditBlock()


def change_indent(cursor: QTextCursor, outdent: bool, unit: str = "  ") -> None:
    blocks = _blocks(cursor)
    cursor.beginEditBlock()
    try:
        for block in blocks:
            text = block.text()
            if outdent:
                n = len(text) - len(text.lstrip(" "))
                _replace_block_prefix(block, min(n, len(unit)), "")
            else:
                _replace_block_prefix(block, 0, unit)
    finally:
        cursor.endEditBlock()


def continue_list(cursor: QTextCursor) -> bool:
    """Enter inside a list item: start the next item, or end the list on an empty item."""
    block = cursor.block()
    text = block.text()
    m = _LIST.match(text)
    if m is None or cursor.hasSelection():
        return False
    cursor.beginEditBlock()
    try:
        if m.end() == len(text):
            _replace_block_prefix(block, m.end(), "")
            return True
        marker = m.group(2)
        if marker[0].isdigit():
            marker = f"{int(marker[:-1]) + 1}{marker[-1]}"
        task = "[ ] " if m.group(3) else ""
        cursor.insertText("\n" + m.group(1) + marker + " " + task)
        return True
    finally:
        cursor.endEditBlock()
