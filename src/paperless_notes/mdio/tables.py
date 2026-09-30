"""GitHub-style tables in the source: recognition around the caret and structural edits.

A table is a header row, a delimiter row with the same number of cells, and the following non-blank rows
that contain a pipe. Pipes escaped with a backslash never split cells, and nothing inside fenced code,
front matter or an HTML comment is a table. Every structural edit is one undo step, keeps each cell's
text and alignment markers, and edits whole lines only, so line breaks and their endings are untouched.
A malformed table is refused rather than guessed at.
"""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QTextBlock, QTextCursor, QTextDocument

from paperless_notes.mdio.edits import EditResult, done, edit_block, insert_text, refuse, replace_text
from paperless_notes.mdio.lexer import is_table_delimiter
from paperless_notes.mdio.source import in_raw_region, index_of, position_of

MAX_TABLE_ROWS = 2_000
MAX_COLUMNS = 64
MAX_NEW_ROWS = 200
TOO_LARGE = "This table is too large to change structurally here."
UNEVEN = "Some rows have a different number of cells. Fix them before changing columns."


@dataclass(frozen=True, slots=True)
class Row:
    indent: str
    leading: bool
    trailing: bool
    cells: tuple[str, ...]
    suffix: str

    def render(self) -> str:
        body = "|".join(self.cells)
        return (
            self.indent + ("|" if self.leading else "") + body + ("|" if self.trailing else "") + self.suffix
        )


@dataclass(frozen=True, slots=True)
class Table:
    header: int
    rows: tuple[Row, ...]

    @property
    def delimiter(self) -> int:
        return self.header + 1

    @property
    def last(self) -> int:
        return self.header + len(self.rows) - 1

    @property
    def columns(self) -> int:
        return len(self.rows[0].cells)


def split_row(text: str) -> Row:
    """Cells between unescaped pipes; the optional outer pipes and trailing spaces are remembered."""
    indent = text[: len(text) - len(text.lstrip(" "))][:3]
    body = text[len(indent) :]
    stripped = body.rstrip()
    suffix = body[len(stripped) :]
    pipes: list[int] = []
    escaped = False
    for i, ch in enumerate(stripped):
        if ch == "|" and not escaped:
            pipes.append(i)
        escaped = ch == "\\" and not escaped
    leading = bool(pipes) and pipes[0] == 0
    trailing = bool(pipes) and pipes[-1] == len(stripped) - 1 and len(stripped) > 1
    bounds = [-1, *pipes, len(stripped)]
    cells = [stripped[bounds[i] + 1 : bounds[i + 1]] for i in range(len(bounds) - 1)]
    if leading:
        cells = cells[1:]
    if trailing:
        cells = cells[:-1]
    return Row(indent, leading, trailing, tuple(cells), suffix)


def _pipe_line(block: QTextBlock) -> bool:
    text = block.text()
    return block.isValid() and bool(text.strip()) and "|" in text


def table_at(cursor: QTextCursor) -> Table | None:
    """The table around the caret, or None when the caret is not in a well-formed table."""
    block = cursor.document().findBlock(cursor.position())
    if not _pipe_line(block):
        return None
    header: QTextBlock | None = None
    probe = block
    steps = 0
    while _pipe_line(probe) and steps <= MAX_TABLE_ROWS:
        following = probe.next()
        if (
            _pipe_line(following)
            and is_table_delimiter(following.text())
            and not is_table_delimiter(probe.text())
        ):
            header = probe
            break
        probe = probe.previous()
        steps += 1
    if header is None or in_raw_region(header):
        return None
    rows: list[Row] = []
    current = header
    while _pipe_line(current) and len(rows) <= MAX_TABLE_ROWS:
        rows.append(split_row(current.text()))
        current = current.next()
    if len(rows) < 2 or len(rows[0].cells) != len(rows[1].cells) or len(rows[0].cells) > MAX_COLUMNS:
        return None
    table = Table(header.blockNumber(), tuple(rows))
    if not table.header <= block.blockNumber() <= table.last:
        return None
    return table


def _cell_bounds(row: Row) -> list[tuple[int, int]]:
    """(start, end) indexes of each cell's text in the rendered row."""
    bounds: list[tuple[int, int]] = []
    position = len(row.indent) + (1 if row.leading else 0)
    for cell in row.cells:
        bounds.append((position, position + len(cell)))
        position += len(cell) + 1
    return bounds


def cell_index(table: Table, cursor: QTextCursor) -> tuple[int, int]:
    """(row, column) of the caret inside ``table``; row 0 is the header."""
    document = cursor.document()
    block = document.findBlock(cursor.position())
    row_number = block.blockNumber() - table.header
    row = table.rows[row_number]
    offset = index_of(block, cursor.position())
    column = 0
    for i, (start, _end) in enumerate(_cell_bounds(row)):
        if offset >= start:
            column = i
    return row_number, min(column, len(row.cells) - 1)


def _content_start(row: Row, column: int) -> int:
    start, _end = _cell_bounds(row)[column]
    cell = row.cells[column]
    if cell.strip():
        return start + len(cell) - len(cell.lstrip(" "))
    return start + min(1, len(cell))


def _block(document: QTextDocument, number: int) -> QTextBlock:
    return document.findBlockByNumber(number)


def _set_row(document: QTextDocument, number: int, row: Row) -> None:
    block = _block(document, number)
    text = row.render()
    if block.text() != text:
        replace_text(document, block.position(), block.position() + block.length() - 1, text)


def _empty_row(template: Row, columns: int) -> Row:
    return Row(template.indent, True, True, tuple("  " for _ in range(columns)), "")


def _check_size(table: Table) -> EditResult | None:
    return refuse(TOO_LARGE) if len(table.rows) > MAX_TABLE_ROWS else None


def _uneven(table: Table) -> bool:
    return any(len(row.cells) != table.columns for row in table.rows)


def _caret_in(document: QTextDocument, number: int, row: Row, column: int) -> int:
    return position_of(_block(document, number), _content_start(row, column))


def insert_table(cursor: QTextCursor, columns: int = 3, rows: int = 2) -> EditResult:
    """A new table below the current line (a blank line first when needed), header text selected."""
    columns = max(1, min(columns, MAX_COLUMNS))
    rows = max(1, min(rows, MAX_NEW_ROWS))
    document = cursor.document()
    block = document.findBlock(cursor.selectionEnd())
    if in_raw_region(block):
        return refuse("A table cannot be inserted inside code or front matter.")
    header = "| " + " | ".join(f"Column {i + 1}" for i in range(columns)) + " |"
    delimiter = "|" + "|".join(" --- " for _ in range(columns)) + "|"
    body = "\n".join("|" + "|".join("  " for _ in range(columns)) + "|" for _ in range(rows))
    source = f"{header}\n{delimiter}\n{body}"
    blank = not block.text().strip()
    with edit_block(document):
        if blank:
            previous = block.previous()
            lead = "\n" if previous.isValid() and previous.text().strip() else ""
            start = block.position() + len(lead)
            replace_text(document, block.position(), block.position() + block.length() - 1, lead + source)
        else:
            at = block.position() + block.length() - 1
            insert_text(document, at, "\n\n" + source)
            start = at + 2
    return done(start + 2, start + 2 + len("Column 1"))


def add_row(cursor: QTextCursor) -> EditResult:
    """A new empty row below the caret's row (below the delimiter when the caret is in the header)."""
    table = table_at(cursor)
    if table is None:
        return refuse("Put the caret in a table first.")
    if (problem := _check_size(table)) is not None:
        return problem
    document = cursor.document()
    row_number, _column = cell_index(table, cursor)
    after = max(row_number, 1)
    anchor = _block(document, table.header + after)
    row = _empty_row(table.rows[0], table.columns)
    with edit_block(document):
        insert_text(document, anchor.position() + anchor.length() - 1, "\n" + row.render())
    return done(_caret_in(document, table.header + after + 1, row, 0))


def remove_row(cursor: QTextCursor) -> EditResult:
    table = table_at(cursor)
    if table is None:
        return refuse("Put the caret in a table first.")
    row_number, column = cell_index(table, cursor)
    if row_number < 2:
        return refuse("The header and delimiter rows cannot be removed. Delete the table text instead.")
    document = cursor.document()
    block = _block(document, table.header + row_number)
    following = block.next()
    with edit_block(document):
        if following.isValid():
            replace_text(document, block.position(), following.position(), "")
        else:
            previous = block.previous()
            replace_text(
                document,
                previous.position() + previous.length() - 1,
                block.position() + block.length() - 1,
                "",
            )
    remaining = [row for i, row in enumerate(table.rows) if i != row_number]
    target = row_number if row_number < len(remaining) else row_number - 1
    if target == 1:
        target = 0
    target_row = remaining[target]
    target_number = table.header + target
    column = min(column, len(target_row.cells) - 1)
    return done(_caret_in(document, target_number, target_row, column))


def add_column(cursor: QTextCursor) -> EditResult:
    """A new empty column to the right of the caret's column."""
    table = table_at(cursor)
    if table is None:
        return refuse("Put the caret in a table first.")
    if (problem := _check_size(table)) is not None:
        return problem
    if _uneven(table):
        return refuse(UNEVEN)
    if table.columns >= MAX_COLUMNS:
        return refuse("This table already has the maximum number of columns.")
    document = cursor.document()
    row_number, column = cell_index(table, cursor)
    updated: list[Row] = []
    for i, row in enumerate(table.rows):
        new = " --- " if i == 1 else "  "
        cells = (*row.cells[: column + 1], new, *row.cells[column + 1 :])
        updated.append(Row(row.indent, row.leading, row.trailing, cells, row.suffix))
    with edit_block(document):
        for i, row in enumerate(updated):
            _set_row(document, table.header + i, row)
    return done(_caret_in(document, table.header + row_number, updated[row_number], column + 1))


def remove_column(cursor: QTextCursor) -> EditResult:
    table = table_at(cursor)
    if table is None:
        return refuse("Put the caret in a table first.")
    if (problem := _check_size(table)) is not None:
        return problem
    if _uneven(table):
        return refuse(UNEVEN)
    if table.columns == 1:
        return refuse("A table needs at least one column. Delete the table text instead.")
    document = cursor.document()
    row_number, column = cell_index(table, cursor)
    updated = [
        Row(r.indent, r.leading, r.trailing, (*r.cells[:column], *r.cells[column + 1 :]), r.suffix)
        for r in table.rows
    ]
    with edit_block(document):
        for i, row in enumerate(updated):
            _set_row(document, table.header + i, row)
    target = min(column, table.columns - 2)
    return done(_caret_in(document, table.header + row_number, updated[row_number], target))


def next_cell(cursor: QTextCursor, backward: bool = False) -> EditResult | None:
    """Tab and Shift+Tab between cells, skipping the delimiter row; Tab in the last cell adds a row.
    None when the caret is not in a table (ordinary Tab)."""
    table = table_at(cursor)
    if table is None:
        return None
    document = cursor.document()
    row_number, column = cell_index(table, cursor)
    order = [(r, c) for r in range(len(table.rows)) if r != 1 for c in range(len(table.rows[r].cells))]
    if (row_number, column) not in order:
        row_number, column = (
            (0, 0) if row_number == 1 else (row_number, len(table.rows[row_number].cells) - 1)
        )
    index = order.index((row_number, column))
    target = index - 1 if backward else index + 1
    if target < 0:
        return done(cursor.position(), cursor.position(), False)
    if target >= len(order):
        if len(table.rows) > MAX_TABLE_ROWS:
            return refuse(TOO_LARGE)
        last = _block(document, table.last)
        row = _empty_row(table.rows[0], table.columns)
        with edit_block(document):
            insert_text(document, last.position() + last.length() - 1, "\n" + row.render())
        return done(_caret_in(document, table.last + 1, row, 0))
    r, c = order[target]
    row = table.rows[r]
    start, _end = _cell_bounds(row)[c]
    content_start = _content_start(row, c)
    cell = row.cells[c]
    content_end = start + len(cell.rstrip(" ")) if cell.strip() else content_start
    block = _block(document, table.header + r)
    return done(position_of(block, content_start), position_of(block, max(content_start, content_end)), False)
