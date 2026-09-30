"""Turns the engine's comparison of the previous and new version into 'changed elsewhere' gutter marks."""

from __future__ import annotations

from collections.abc import Callable

from paperless_notes.core.diffing import DiffLimits, DiffModel, Op, sequence_opcodes
from paperless_notes.ui.editor.note_editor import ChangeMark

PREVIEW_LINES = 4


def line_mapper(theirs: str, buffer: str) -> Callable[[int], int]:
    """Maps 0-based line numbers of the new file version to lines of the (merged) buffer."""
    codes, _ = sequence_opcodes(theirs.split("\n"), buffer.split("\n"), DiffLimits())
    table: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in codes:
        for offset in range(i2 - i1):
            table[i1 + offset] = j1 + offset if tag == "equal" else j1 + min(offset, max(0, j2 - j1 - 1))
    last = len(buffer.split("\n")) - 1

    def mapped(line: int) -> int:
        return table.get(line, min(line, last))

    return mapped


def model_mapper(model: DiffModel | None) -> Callable[[int], int] | None:
    """Maps 0-based lines of a comparison's left text to its right text using only the model's hunks."""
    if model is None or model.truncated:
        return None
    exact: dict[int, int] = {}
    shifts: list[tuple[int, int]] = []
    for hunk in model.hunks:
        cursor = hunk.right_start - 1
        for line in hunk.lines:
            if line.right_no is not None:
                if line.left_no is not None:
                    exact[line.left_no - 1] = line.right_no - 1
                cursor = line.right_no
            elif line.left_no is not None:
                exact[line.left_no - 1] = cursor
        left_end = hunk.left_start - 1 + hunk.left_count
        shifts.append((left_end, hunk.right_start - 1 + hunk.right_count - left_end))

    def mapped(line: int) -> int:
        if line in exact:
            return exact[line]
        delta = 0
        for start, shift in shifts:
            if line < start:
                break
            delta = shift
        return line + delta

    return mapped


def _preview(lines: list[str]) -> str:
    return "\n".join(lines[:PREVIEW_LINES]) + ("\n..." if len(lines) > PREVIEW_LINES else "")


def _mark(
    before: list[str], after: list[str], first: int | None, anchor: int, mapper: Callable[[int], int] | None
) -> ChangeMark:
    start = max(0, (first if first is not None else anchor) - 1)
    if mapper is not None:
        start = max(0, mapper(start))
    return ChangeMark(start, start + max(1, len(after)), _preview(before), _preview(after))


def change_marks(model: DiffModel | None, mapper: Callable[[int], int] | None = None) -> list[ChangeMark]:
    """One mark per changed run: added or replaced lines, or the line after a removal."""
    if model is None or model.identical:
        return []
    marks: list[ChangeMark] = []
    for hunk in model.hunks:
        before: list[str] = []
        after: list[str] = []
        first: int | None = None
        last_right = hunk.right_start - 1
        for line in hunk.lines:
            if line.op is Op.EQUAL:
                if before or after:
                    marks.append(_mark(before, after, first, line.right_no or last_right + 1, mapper))
                    before, after, first = [], [], None
                last_right = line.right_no or last_right
            elif line.op is Op.DELETE:
                before.append(line.text)
            else:
                after.append(line.text)
                first = line.right_no if first is None else first
                last_right = line.right_no or last_right
        if before or after:
            marks.append(_mark(before, after, first, max(1, last_right), mapper))
    return marks
