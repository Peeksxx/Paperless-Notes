"""Source transformations behind every authoring surface (menus, slash, bubble, shortcuts, command search).

Contract for every function: it derives its range from the cursor and the lexer, changes only that range,
does all of it inside one edit block (one undo step), and leaves unrelated characters, line breaks and
their tracked endings untouched. It returns the selection to show afterwards, or refuses with a plain
message and no change when the request is ambiguous or does not apply (fenced code, a link address,
several lines where one is needed). Read-only checks belong to the caller (``ui.editor.authoring``).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from PySide6.QtGui import QTextBlock, QTextCursor, QTextDocument

from paperless_notes.core.security.links import link_allowed
from paperless_notes.mdio import commands
from paperless_notes.mdio.lexer import Span, State, Style
from paperless_notes.mdio.source import (
    in_fence,
    in_raw_region,
    index_of,
    line_result,
    position_of,
    selected_blocks,
    state_before,
)

MAX_TASK_SCAN_CHARS = 2_000_000
INLINE = {
    "bold": ("**", Style.STRONG),
    "italic": ("*", Style.EMPHASIS),
    "strike": ("~~", Style.STRIKE),
    "code": ("`", Style.CODE),
}
_FORMATTED = Style.STRONG | Style.EMPHASIS | Style.STRIKE | Style.CODE | Style.LINK | Style.URL | Style.HTML
_LITERAL = Style.CODE | Style.CODE_BLOCK | Style.URL | Style.HTML | Style.META
_LIST_ITEM = re.compile(r"([ \t]*)([-+*]|\d{1,9}[.)])([ \t]+|$)(\[[ xX]\](?:[ \t]+|$))?")
_TASK_LINE = re.compile(r"[ \t]*(?:>[ \t]?)*[ \t]*(?:[-+*]|\d{1,9}[.)])[ \t]+\[([ xX])\](?:[ \t]|$)")
_FENCE_LINE = re.compile(r" {0,3}(`{3,}|~{3,})")

ONE_LINE = "Select text within one line to use this command."
IN_CODE = "This does not apply inside code, a link address or front matter."
OVERLAP = "The selection overlaps other formatting. Select whole words or a whole formatted run."


@dataclass(frozen=True, slots=True)
class EditResult:
    """Outcome of a command. ``anchor`` and ``position`` are the selection to show afterwards."""

    ok: bool
    message: str = ""
    anchor: int = -1
    position: int = -1
    changed: bool = False


def refuse(message: str) -> EditResult:
    return EditResult(False, message)


def done(anchor: int, position: int | None = None, changed: bool = True) -> EditResult:
    return EditResult(True, "", anchor, anchor if position is None else position, changed)


@contextmanager
def edit_block(document: QTextDocument) -> Iterator[QTextCursor]:
    """One undo step; nested blocks join the outer one."""
    cursor = QTextCursor(document)
    cursor.beginEditBlock()
    try:
        yield cursor
    finally:
        cursor.endEditBlock()


def insert_text(document: QTextDocument, position: int, text: str) -> None:
    cursor = QTextCursor(document)
    cursor.setPosition(position)
    cursor.insertText(text)


def replace_text(document: QTextDocument, start: int, end: int, text: str) -> None:
    cursor = QTextCursor(document)
    cursor.setPosition(start)
    cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
    cursor.insertText(text)


def _single_block(cursor: QTextCursor) -> QTextBlock | None:
    document = cursor.document()
    block = document.findBlock(cursor.selectionStart())
    return block if document.findBlock(cursor.selectionEnd()) == block else None


def _inside_literal(spans: list[Span], start: int, end: int) -> bool:
    """True when the range touches text that is code, a URL, HTML or metadata."""
    for span in spans:
        if not span.style & _LITERAL:
            continue
        if start == end and span.start < start < span.end:
            return True
        if start < end and span.start < end and start < span.end:
            return True
    return False


def _crosses(spans: list[Span], start: int, end: int) -> bool:
    """True when a formatted run or marker is cut by the range boundary."""
    for span in spans:
        if not span.style & (_FORMATTED | Style.MARKER) or span.end - span.start == 0:
            continue
        partial_left = span.start < start < span.end
        partial_right = span.start < end < span.end
        if (partial_left or partial_right) and not (span.start <= start and end <= span.end):
            return True
        if span.style & Style.MARKER and span.start < start < span.end:
            return True
        if span.style & Style.MARKER and span.start < end < span.end:
            return True
    return False


def _stray_markers(text: str, spans: list[Span], start: int, end: int, char: str) -> bool:
    covered = bytearray(len(text))
    for span in spans:
        if span.style & (Style.MARKER | Style.ESCAPE | _LITERAL):
            covered[max(0, span.start) : max(0, span.end)] = b"\x01" * max(0, span.end - span.start)
    return any(text[i] == char and not covered[i] for i in range(start, end))


def toggle_inline(cursor: QTextCursor, kind: str) -> EditResult:
    """Wrap the selection in a marker pair, or unwrap an exactly matching pair; with no selection insert
    the pair and place the caret inside."""
    marker, style = INLINE[kind]
    size = len(marker)
    block = _single_block(cursor)
    if block is None:
        return refuse(ONE_LINE)
    if in_raw_region(block):
        return refuse(IN_CODE)
    document = cursor.document()
    text = block.text()
    spans = line_result(block).spans
    start = index_of(block, cursor.selectionStart())
    end = index_of(block, cursor.selectionEnd())
    if start == end:
        if _inside_literal(spans, start, start):
            return refuse(IN_CODE)
        with edit_block(document):
            insert_text(document, position_of(block, start), marker * 2)
        caret = position_of(block, start + size)
        return done(caret)
    for span in spans:
        if not span.style & style or span.style & Style.MARKER:
            continue
        if (span.start, span.end) == (start - size, end + size) and _pair_at(text, start - size, end, marker):
            with edit_block(document):
                replace_text(document, position_of(block, end), position_of(block, end + size), "")
                replace_text(document, position_of(block, start - size), position_of(block, start), "")
            return done(position_of(block, start - size), position_of(block, end - size))
        if (
            (span.start, span.end) == (start, end)
            and end - start > 2 * size
            and _pair_at(text, start, end - size, marker)
        ):
            with edit_block(document):
                replace_text(document, position_of(block, end - size), position_of(block, end), "")
                replace_text(document, position_of(block, start), position_of(block, start + size), "")
            return done(position_of(block, start), position_of(block, end - 2 * size))
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    if start == end:
        return refuse("Select some text to format.")
    if _inside_literal(spans, start, end) and kind != "code":
        return refuse(IN_CODE)
    if _crosses(spans, start, end):
        return refuse(OVERLAP)
    if kind == "code" and "`" in text[start:end]:
        return refuse("The selection contains backticks, so inline code would be ambiguous.")
    if _stray_markers(text, spans, start, end, marker[0]):
        return refuse(OVERLAP)
    with edit_block(document):
        insert_text(document, position_of(block, end), marker)
        insert_text(document, position_of(block, start), marker)
    return done(position_of(block, start + size), position_of(block, end + size))


def _pair_at(text: str, open_at: int, close_at: int, marker: str) -> bool:
    size = len(marker)
    if text[open_at : open_at + size] != marker or text[close_at : close_at + size] != marker:
        return False
    char = marker[0]
    before = text[open_at - 1] if open_at > 0 else ""
    after = text[close_at + size] if close_at + size < len(text) else ""
    return before != char and after != char


def _link_span(spans: list[Span], index: int) -> tuple[Span, Span | None] | None:
    """The inline link whose label or address contains ``index``, with its address span."""
    for span in spans:
        if not span.style & Style.LINK or span.style & Style.MARKER:
            continue
        destination = next(
            (s for s in spans if s.start == span.end + 1 and s.style & Style.URL and s.style & Style.MARKER),
            None,
        )
        in_label = span.start - 1 <= index <= span.end + 1
        in_destination = destination is not None and destination.start <= index <= destination.end
        if in_label or in_destination:
            return span, destination
    return None


def destination_text(url: str) -> str:
    """A link destination that stays one token: angle brackets when it has spaces or parentheses."""
    return f"<{url}>" if any(c in url for c in " ()<>") else url


def edit_link(cursor: QTextCursor) -> EditResult:
    """Ctrl+K: select an existing link's address, turn a selected URL or text into a link, or insert an
    empty ``[]()`` with the caret in the label. No placeholder text is ever inserted."""
    block = _single_block(cursor)
    if block is None:
        return refuse(ONE_LINE)
    if in_raw_region(block):
        return refuse(IN_CODE)
    document = cursor.document()
    text = block.text()
    spans = line_result(block).spans
    start = index_of(block, cursor.selectionStart())
    end = index_of(block, cursor.selectionEnd())
    existing = _link_span(spans, start)
    if existing is not None and (start == end or _link_span(spans, end) == existing):
        _label, destination = existing
        if destination is not None and destination.end - destination.start >= 2:
            return done(
                position_of(block, destination.start + 1), position_of(block, destination.end - 1), False
            )
    if start == end:
        if _inside_literal(spans, start, start):
            return refuse(IN_CODE)
        with edit_block(document):
            insert_text(document, position_of(block, start), "[]()")
        return done(position_of(block, start + 1))
    selected = text[start:end]
    if _crosses(spans, start, end):
        return refuse(OVERLAP)
    if link_allowed(selected.strip()):
        url = selected.strip()
        with edit_block(document):
            replace_text(
                document, position_of(block, start), position_of(block, end), f"[]({destination_text(url)})"
            )
        return done(position_of(block, start + 1))
    if _inside_literal(spans, start, end):
        return refuse(IN_CODE)
    if selected.count("[") != selected.count("]"):
        return refuse("The selection has unmatched brackets, so the link would be ambiguous.")
    with edit_block(document):
        replace_text(document, position_of(block, start), position_of(block, end), f"[{selected}]()")
    return done(position_of(block, start + len(selected) + 3))


def link_selection_to(cursor: QTextCursor, url: str) -> EditResult | None:
    """Pasting a URL over selected text in one line makes a link; None means paste normally."""
    if not cursor.hasSelection() or not link_allowed(url.strip()):
        return None
    block = _single_block(cursor)
    if block is None or in_raw_region(block):
        return None
    spans = line_result(block).spans
    start = index_of(block, cursor.selectionStart())
    end = index_of(block, cursor.selectionEnd())
    selected = block.text()[start:end]
    if (
        link_allowed(selected.strip())
        or _inside_literal(spans, start, end)
        or _crosses(spans, start, end)
        or "\n" in selected
        or selected.count("[") != selected.count("]")
    ):
        return None
    document = cursor.document()
    replacement = f"[{selected}]({destination_text(url.strip())})"
    with edit_block(document):
        replace_text(document, position_of(block, start), position_of(block, end), replacement)
    return done(position_of(block, start + len(replacement)))


def _block_command(cursor: QTextCursor, action: str, argument: int = 0) -> EditResult:
    blocks = selected_blocks(cursor)
    if any(in_raw_region(b) for b in blocks):
        return refuse(IN_CODE)
    work = QTextCursor(cursor)
    with edit_block(cursor.document()):
        if action == "heading":
            commands.toggle_heading(work, argument)
        elif action == "quote":
            commands.toggle_quote(work)
        else:
            commands.toggle_list(work, commands.ListKind(action))
    last = blocks[-1]
    if cursor.hasSelection():
        return done(blocks[0].position(), last.position() + last.length() - 1)
    return done(last.position() + last.length() - 1)


def set_heading(cursor: QTextCursor, level: int) -> EditResult:
    return _block_command(cursor, "heading", level)


def set_quote(cursor: QTextCursor) -> EditResult:
    return _block_command(cursor, "quote")


def set_list(cursor: QTextCursor, kind: str) -> EditResult:
    """``kind`` is ``bullet``, ``ordered`` or ``task``."""
    return _block_command(cursor, kind)


def _blank(block: QTextBlock) -> bool:
    return not block.text().strip()


def insert_rule(cursor: QTextCursor) -> EditResult:
    """A thematic break on its own line, with a blank line above so it never becomes a heading. The caret
    moves to the line below, so the rule is drawn at once."""
    block = cursor.document().findBlock(cursor.selectionEnd())
    if in_raw_region(block):
        return refuse(IN_CODE)
    document = cursor.document()
    with edit_block(document):
        if _blank(block):
            previous = block.previous()
            prefix = "\n" if previous.isValid() and not _blank(previous) else ""
            replace_text(document, block.position(), block.position() + block.length() - 1, prefix + "---")
            rule_end = block.position() + len(prefix) + 3
        else:
            rule_end = block.position() + block.length() - 1
            insert_text(document, rule_end, "\n\n---")
            rule_end += 5
        after = document.findBlock(rule_end).next()
        if after.isValid() and _blank(after):
            end = after.position()
        else:
            insert_text(document, rule_end, "\n")
            end = rule_end + 1
    return done(end)


def insert_code_block(cursor: QTextCursor, language: str = "") -> EditResult:
    """Fence the selected lines, or insert an empty fenced block with the caret on its inner line."""
    document = cursor.document()
    blocks = selected_blocks(cursor)
    if any(in_fence(b) for b in blocks):
        return refuse("The caret is already inside a code block.")
    opening = "```" + language
    with edit_block(document):
        if cursor.hasSelection():
            last = blocks[-1]
            insert_text(document, last.position() + last.length() - 1, "\n```")
            insert_text(document, blocks[0].position(), opening + "\n")
            return done(blocks[0].position() + len(opening))
        block = blocks[0]
        if _blank(block):
            replace_text(
                document, block.position(), block.position() + block.length() - 1, opening + "\n\n```"
            )
            inner = block.position() + len(opening) + 1
        else:
            at = block.position() + block.length() - 1
            insert_text(document, at, "\n" + opening + "\n\n```")
            inner = at + 1 + len(opening) + 1
    return done(inner)


def insert_image_reference(cursor: QTextCursor, reference: str, alt: str) -> EditResult:
    """``![alt](reference)`` at the caret (replacing a selection); call only after the asset exists."""
    block = _single_block(cursor)
    if block is None:
        return refuse(ONE_LINE)
    if in_raw_region(block):
        return refuse(IN_CODE)
    spans = line_result(block).spans
    start = index_of(block, cursor.selectionStart())
    end = index_of(block, cursor.selectionEnd())
    if _inside_literal(spans, start, end):
        return refuse(IN_CODE)
    safe_alt = alt.replace("[", "").replace("]", "").replace("\n", " ").strip()
    text = f"![{safe_alt}]({destination_text(reference)})"
    document = cursor.document()
    with edit_block(document):
        replace_text(document, position_of(block, start), position_of(block, end), text)
    return done(position_of(block, start + len(text)))


def continue_list(cursor: QTextCursor) -> EditResult | None:
    """Enter in a list item: continue it with the same indentation, or leave the level on an empty item.
    None means an ordinary line break."""
    if cursor.hasSelection():
        return None
    block = cursor.block()
    if in_raw_region(block):
        return None
    text = block.text()
    match = _LIST_ITEM.match(text)
    if match is None:
        return None
    caret = index_of(block, cursor.position())
    if caret < match.end():
        return None
    document = cursor.document()
    indent, marker, task = match.group(1), match.group(2), match.group(4)
    with edit_block(document):
        if match.end() == len(text) or not text[match.end() :].strip():
            if indent:
                keep = indent[: max(0, len(indent) - _indent_unit(marker, indent))]
                replace_text(document, block.position(), block.position() + len(indent), keep)
                return done(block.position() + block.length() - 1)
            replace_text(document, block.position(), block.position() + block.length() - 1, "")
            return done(block.position())
        if marker[0].isdigit():
            marker = f"{int(marker[:-1]) + 1}{marker[-1]}"
        prefix = indent + marker + " " + ("[ ] " if task else "")
        position = position_of(block, caret)
        insert_text(document, position, "\n" + prefix)
    return done(position + 1 + len(prefix))


def _indent_unit(marker: str, indent: str) -> int:
    if indent.endswith("\t"):
        return 1
    return len(marker) + 1 if marker[0].isdigit() else 2


def indent_list(cursor: QTextCursor, outdent: bool) -> EditResult | None:
    """Tab or Shift+Tab when every touched line is a list item; None means ordinary Tab behaviour."""
    blocks = selected_blocks(cursor)
    if any(in_raw_region(b) for b in blocks):
        return None
    matches = [_LIST_ITEM.match(b.text()) for b in blocks]
    if any(m is None for m in matches):
        return None
    document = cursor.document()
    anchor_block = document.findBlock(cursor.anchor())
    position_block = document.findBlock(cursor.position())
    anchor_offset = cursor.anchor() - anchor_block.position()
    position_offset = cursor.position() - position_block.position()
    shifts: dict[int, int] = {}
    with edit_block(document):
        for block, match in zip(blocks, matches, strict=True):
            if match is None:
                continue
            indent, marker = match.group(1), match.group(2)
            if outdent:
                if not indent:
                    shifts[block.blockNumber()] = 0
                    continue
                remove = (
                    1
                    if indent[0] == "\t"
                    else min(len(indent) - len(indent.lstrip(" ")), _indent_unit(marker, ""))
                )
                remove = max(remove, 1)
                replace_text(document, block.position(), block.position() + remove, "")
                shifts[block.blockNumber()] = -remove
            else:
                unit = "\t" if indent.startswith("\t") else " " * _indent_unit(marker, "")
                insert_text(document, block.position(), unit)
                shifts[block.blockNumber()] = len(unit)
    anchor = anchor_block.position() + _shifted(anchor_offset, shifts.get(anchor_block.blockNumber(), 0))
    position = position_block.position() + _shifted(
        position_offset, shifts.get(position_block.blockNumber(), 0)
    )
    return done(anchor, position)


def _shifted(offset: int, shift: int) -> int:
    """A caret at a line start stays there when the line is indented, so whole-line selections stay whole."""
    if offset == 0 and shift > 0:
        return 0
    return max(0, offset + shift)


def _set_block_texts(document: QTextDocument, blocks: list[QTextBlock], texts: list[str]) -> None:
    for block, text in zip(blocks, texts, strict=True):
        if block.text() != text:
            replace_text(document, block.position(), block.position() + block.length() - 1, text)


def move_lines(cursor: QTextCursor, up: bool) -> EditResult:
    """Move the touched lines past their neighbour. Line breaks stay where they are, so tracked line
    endings keep their positions; a move at the document edge changes nothing."""
    document = cursor.document()
    blocks = selected_blocks(cursor)
    neighbour = blocks[0].previous() if up else blocks[-1].next()
    anchor, position = cursor.anchor(), cursor.position()
    if not neighbour.isValid():
        return done(anchor, position, False)
    region = [neighbour, *blocks] if up else [*blocks, neighbour]
    texts = [b.text() for b in region]
    moved = [*texts[1:], texts[0]] if up else [texts[-1], *texts[:-1]]
    anchor_line, anchor_offset = _line_offset(document, anchor)
    position_line, position_offset = _line_offset(document, position)
    delta = -1 if up else 1
    with edit_block(document):
        _set_block_texts(document, region, moved)
    return done(
        _position(document, anchor_line + delta, anchor_offset),
        _position(document, position_line + delta, position_offset),
    )


def duplicate_lines(cursor: QTextCursor) -> EditResult:
    """Copy the touched lines below themselves; the selection moves to the copy."""
    document = cursor.document()
    blocks = selected_blocks(cursor)
    last = blocks[-1]
    copy = "\n".join(b.text() for b in blocks)
    anchor_line, anchor_offset = _line_offset(document, cursor.anchor())
    position_line, position_offset = _line_offset(document, cursor.position())
    with edit_block(document):
        insert_text(document, last.position() + last.length() - 1, "\n" + copy)
    count = len(blocks)
    return done(
        _position(document, anchor_line + count, anchor_offset),
        _position(document, position_line + count, position_offset),
    )


def _line_offset(document: QTextDocument, position: int) -> tuple[int, int]:
    block = document.findBlock(position)
    return block.blockNumber(), position - block.position()


def _position(document: QTextDocument, line: int, offset: int) -> int:
    block = document.findBlockByNumber(line)
    return block.position() + min(offset, block.length() - 1)


def code_block_text(cursor: QTextCursor, max_lines: int = 20_000) -> str | None:
    """The code inside the fenced block around the caret, without fences or info string; None outside."""
    opener = cursor.block()
    if not in_fence(opener):
        return None
    for _ in range(max_lines):
        if _opens(opener):
            break
        opener = opener.previous()
        if not opener.isValid():
            return None
    else:
        return None
    lines: list[str] = []
    current = opener.next()
    while current.isValid() and len(lines) < max_lines and not _closes(current):
        lines.append(current.text())
        current = current.next()
    return "\n".join(lines)


def _opens(block: QTextBlock) -> bool:
    return (
        State.fence_info(state_before(block)) is None
        and State.fence_info(line_result(block).state) is not None
    )


def _closes(block: QTextBlock) -> bool:
    return (
        State.fence_info(state_before(block)) is not None
        and State.fence_info(line_result(block).state) is None
    )


def count_tasks(document: QTextDocument) -> tuple[int, int] | None:
    """(done, total) task items outside fenced code; None for notes too large to count cheaply."""
    if document.characterCount() > MAX_TASK_SCAN_CHARS:
        return None
    done_count = total = 0
    fence: str | None = None
    block = document.firstBlock()
    while block.isValid():
        text = block.text()
        fence_match = _FENCE_LINE.match(text)
        if fence is None and fence_match:
            fence = fence_match.group(1)[0] * len(fence_match.group(1))
        elif fence is not None:
            stripped = text.strip()
            if stripped.startswith(fence) and not stripped.strip(fence[0]):
                fence = None
        else:
            task = _TASK_LINE.match(text)
            if task:
                total += 1
                done_count += task.group(1) in "xX"
        block = block.next()
    return done_count, total


MAX_SLASH_QUERY = 32


@dataclass(frozen=True, slots=True)
class SlashQuery:
    """A slash command being typed: document positions of the slash and the caret, and the text between."""

    start: int
    end: int
    text: str


def slash_query(cursor: QTextCursor) -> SlashQuery | None:
    """The ``/query`` just before the caret, when a slash menu may apply there.

    The slash must start the line or follow whitespace (so ``a/b`` and ``\\/`` never trigger) and must not
    sit in fenced code, inline code, a link address, HTML or front matter. Only the current line is read.
    """
    if cursor.hasSelection():
        return None
    block = cursor.block()
    text = block.text()
    caret = index_of(block, cursor.position())
    slash = text.rfind("/", 0, caret)
    if slash < 0:
        return None
    query = text[slash + 1 : caret]
    if len(query) > MAX_SLASH_QUERY or any(not (c.isalnum() or c == "-") for c in query):
        return None
    if slash > 0 and text[slash - 1] not in " \t":
        return None
    if _open_backtick(text, slash):
        return None
    if in_raw_region(block) or _inside_literal(line_result(block).spans, slash, slash + 1):
        return None
    return SlashQuery(position_of(block, slash), position_of(block, caret), query)


def _open_backtick(text: str, end: int) -> bool:
    """True when an unescaped backtick before ``end`` has not been closed: the user is typing code."""
    count = 0
    escaped = False
    for ch in text[:end]:
        if ch == "`" and not escaped:
            count += 1
        escaped = ch == "\\" and not escaped
    return count % 2 == 1


def remove_range(document: QTextDocument, start: int, end: int) -> None:
    """Delete ``start`` to ``end``; used inside a caller's edit block to drop a typed slash query."""
    replace_text(document, start, end, "")
