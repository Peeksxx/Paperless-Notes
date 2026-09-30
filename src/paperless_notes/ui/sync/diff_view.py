"""Shows one DiffModel inline or side by side. The model is rendered as given and never recomputed, so an
open viewer keeps showing the exact comparison it was opened with even if the file changes again."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QButtonGroup,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core.diffing import DiffModel, Op
from paperless_notes.ui.theme.icons import glyph_icon
from paperless_notes.ui.theme.tokens import Theme


@dataclass(frozen=True, slots=True)
class Row:
    """One display row: a line on the left, the right, or both. ``hunk`` counts from 0."""

    left_no: int | None
    left: str | None
    right_no: int | None
    right: str | None
    op: Op
    hunk: int


def _pair(
    rows: list[Row], removed: list[tuple[int | None, str]], added: list[tuple[int | None, str]], hunk: int
) -> None:
    for i in range(max(len(removed), len(added))):
        left_no, left = removed[i] if i < len(removed) else (None, None)
        right_no, right = added[i] if i < len(added) else (None, None)
        rows.append(Row(left_no, left, right_no, right, Op.INSERT if left is None else Op.DELETE, hunk))
    removed.clear()
    added.clear()


def side_by_side_rows(model: DiffModel) -> list[Row]:
    """Pairs removed and added runs into replacement rows; unchanged lines sit on both sides."""
    rows: list[Row] = []
    for number, hunk in enumerate(model.hunks):
        removed: list[tuple[int | None, str]] = []
        added: list[tuple[int | None, str]] = []
        for line in hunk.lines:
            if line.op is Op.EQUAL:
                _pair(rows, removed, added, number)
                rows.append(Row(line.left_no, line.text, line.right_no, line.text, Op.EQUAL, number))
            elif line.op is Op.DELETE:
                removed.append((line.left_no, line.text))
            else:
                added.append((line.right_no, line.text))
        _pair(rows, removed, added, number)
    return rows


def summary(model: DiffModel | None) -> str:
    if model is None:
        return "One of the versions is not available to compare."
    if model.identical:
        return "The versions are identical."
    notes = []
    if model.line_endings_only:
        notes.append("only line endings differ")
    elif model.whitespace_only:
        notes.append("only spaces differ")
    if model.truncated:
        notes.append("a very large change is shown in summary form")
    s = model.stats
    text = f"{s.added} line{'s' if s.added != 1 else ''} added, {s.removed} removed, {s.hunks} change"
    text += "s" if s.hunks != 1 else ""
    return text + (f" ({'; '.join(notes)})" if notes else "")


def _no(value: int | None) -> str:
    return f"{value:>5}" if value is not None else "     "


class _Pane(QPlainTextEdit):
    def __init__(self, name: str, wrap: bool) -> None:
        super().__init__()
        self.setObjectName("DiffPane")
        self.setReadOnly(True)
        self.setAccessibleName(name)
        self.setLineWrapMode(
            QPlainTextEdit.LineWrapMode.WidgetWidth if wrap else QPlainTextEdit.LineWrapMode.NoWrap
        )
        self.hunk_blocks: list[int] = []

    def fill(self, lines: list[tuple[str, str | None, int]], theme: Theme) -> None:
        """``lines`` are (text, kind, hunk) with kind 'add', 'remove', 'blank' or None."""
        p = theme.palette
        colors = {
            "add": QColor(p.diff_add_background),
            "remove": QColor(p.diff_remove_background),
            "blank": QColor(p.code_background),
        }
        self.setPlainText("\n".join(text for text, _, _ in lines))
        selections: list[QTextEdit.ExtraSelection] = []
        block = self.document().firstBlock()
        seen: set[int] = set()
        self.hunk_blocks = []
        for text_kind in lines:
            _, kind, hunk = text_kind
            if hunk not in seen and kind in ("add", "remove"):
                seen.add(hunk)
                self.hunk_blocks.append(block.blockNumber())
            if kind in colors:
                selection: Any = QTextEdit.ExtraSelection()
                fmt = QTextCharFormat()
                fmt.setBackground(colors[kind])
                fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
                selection.format = fmt
                cursor = QTextCursor(block)
                selection.cursor = cursor
                selections.append(selection)
            block = block.next()
        self.setExtraSelections(selections)

    def go_to_block(self, number: int) -> None:
        block = self.document().findBlockByNumber(number)
        cursor = QTextCursor(block)
        self.setTextCursor(cursor)
        self.centerCursor()


class DiffView(QWidget):
    """Inline or side-by-side rendering of one DiffModel with hunk navigation (Alt+Down, Alt+Up)."""

    def __init__(
        self, model: DiffModel | None, theme: Theme, note: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.model = model
        self._theme = theme
        self._current = -1
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        s = theme.spacing
        header = QHBoxLayout()
        header.setSpacing(s.md)
        labels = (
            f"{model.left_label} compared with {model.right_label}" if model is not None else "Comparison"
        )
        self.labels = QLabel(labels[:1].upper() + labels[1:])
        self.labels.setProperty("role", "heading")
        described = summary(model)
        self.summary = QLabel(described if described.endswith(".") else described + ".")
        self.summary.setProperty("role", "secondary")
        self.summary.setWordWrap(True)
        self.note = QLabel(note)
        self.note.setWordWrap(True)
        self.note.setVisible(bool(note))
        header.addWidget(self.labels)
        header.addStretch(1)
        p = theme.palette
        self.inline_button = QToolButton()
        self.inline_button.setCheckable(True)
        self.inline_button.setIcon(glyph_icon("inline", p.text_secondary, p.text))
        self.inline_button.setToolTip("Inline view: both versions in one column")
        self.inline_button.setAccessibleName("Inline view")
        self.split_button = QToolButton()
        self.split_button.setCheckable(True)
        self.split_button.setIcon(glyph_icon("side_by_side", p.text_secondary, p.text))
        self.split_button.setToolTip("Side-by-side view")
        self.split_button.setAccessibleName("Side-by-side view")
        group = QButtonGroup(self)
        group.setExclusive(True)
        group.addButton(self.inline_button)
        group.addButton(self.split_button)
        self.previous_button = QPushButton("Previous change")
        self.previous_button.setToolTip("Previous change (Alt+Up)")
        self.next_button = QPushButton("Next change")
        self.next_button.setToolTip("Next change (Alt+Down)")
        self.previous_button.clicked.connect(lambda: self.move_hunk(-1))
        self.next_button.clicked.connect(lambda: self.move_hunk(1))
        for w in (self.inline_button, self.split_button, self.previous_button, self.next_button):
            header.addWidget(w)
        layout.addLayout(header)
        layout.addWidget(self.summary)
        layout.addWidget(self.note)
        self.stack = QStackedWidget()
        self.inline = _Pane("Inline comparison", wrap=True)
        split = QWidget()
        split_layout = QHBoxLayout(split)
        split_layout.setContentsMargins(0, 0, 0, 0)
        self.left = _Pane(model.left_label if model else "Left", wrap=False)
        self.right = _Pane(model.right_label if model else "Right", wrap=False)
        split_layout.addWidget(self.left)
        split_layout.addWidget(self.right)
        self.left.verticalScrollBar().valueChanged.connect(self.right.verticalScrollBar().setValue)
        self.right.verticalScrollBar().valueChanged.connect(self.left.verticalScrollBar().setValue)
        self.stack.addWidget(self.inline)
        self.stack.addWidget(split)
        layout.addWidget(self.stack, 1)
        self.inline_button.clicked.connect(lambda: self.set_mode("inline"))
        self.split_button.clicked.connect(lambda: self.set_mode("side"))
        QShortcut(QKeySequence("Alt+Down"), self, self._next_hunk)
        QShortcut(QKeySequence("Alt+Up"), self, self._previous_hunk)
        self._render()
        self.set_mode("inline")

    def _render(self) -> None:
        model = self.model
        if model is None or model.identical:
            message = summary(model)
            for pane in (self.inline, self.left, self.right):
                pane.setPlainText(message)
            for w in (self.previous_button, self.next_button):
                w.setEnabled(False)
            return
        inline: list[tuple[str, str | None, int]] = []
        for number, hunk in enumerate(model.hunks):
            inline.append(
                (f"@@ line {hunk.left_start} before, line {hunk.right_start} after @@", None, number)
            )
            for line in hunk.lines:
                mark = {Op.EQUAL: " ", Op.DELETE: "-", Op.INSERT: "+"}[line.op]
                kind = {Op.EQUAL: None, Op.DELETE: "remove", Op.INSERT: "add"}[line.op]
                inline.append((f"{_no(line.left_no)} {_no(line.right_no)} {mark} {line.text}", kind, number))
        self.inline.fill(inline, self._theme)
        rows = side_by_side_rows(model)
        left: list[tuple[str, str | None, int]] = []
        right: list[tuple[str, str | None, int]] = []
        for row in rows:
            changed = row.op is not Op.EQUAL
            left.append(
                (
                    f"{_no(row.left_no)} {row.left}" if row.left is not None else "",
                    "remove" if changed and row.left is not None else ("blank" if changed else None),
                    row.hunk,
                )
            )
            right.append(
                (
                    f"{_no(row.right_no)} {row.right}" if row.right is not None else "",
                    "add" if changed and row.right is not None else ("blank" if changed else None),
                    row.hunk,
                )
            )
        self.left.fill(left, self._theme)
        self.right.fill(right, self._theme)

    def set_mode(self, mode: str) -> None:
        side = mode == "side"
        self.stack.setCurrentIndex(1 if side else 0)
        self.split_button.setChecked(side)
        self.inline_button.setChecked(not side)

    @property
    def mode(self) -> str:
        return "side" if self.stack.currentIndex() == 1 else "inline"

    def _next_hunk(self) -> None:
        self.move_hunk(1)

    def _previous_hunk(self) -> None:
        self.move_hunk(-1)

    def hunk_count(self) -> int:
        return len(self.model.hunks) if self.model is not None and not self.model.identical else 0

    def move_hunk(self, step: int) -> int:
        count = self.hunk_count()
        if not count:
            return -1
        self._current = max(0, min(count - 1, self._current + step))
        if self.mode == "side":
            for pane in (self.left, self.right):
                if self._current < len(pane.hunk_blocks):
                    pane.go_to_block(pane.hunk_blocks[self._current])
        elif self._current < len(self.inline.hunk_blocks):
            self.inline.go_to_block(self.inline.hunk_blocks[self._current])
        return self._current


class DiffDialog(QDialog):
    def __init__(
        self, title: str, model: DiffModel | None, theme: Theme, note: str = "", parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.resize(960, 640)
        layout = QVBoxLayout(self)
        self.view = DiffView(model, theme, note, self)
        layout.addWidget(self.view)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close)
        layout.addLayout(row)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, False)
