"""The conflict resolver: base, yours and theirs side by side with a live, editable result.

Only the engine's four paths are offered: keep mine, keep theirs, keep both (your version saved as a
separate copy) and use the edited result. A side that cannot be read is shown as unavailable, never as an
empty text, and a resolution the engine does not complete leaves the resolver open with its message.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QMetaObject, QObject
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor, QTextFormat
from PySide6.QtWidgets import (
    QDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core.diffing import Op, diff_lines
from paperless_notes.core.merge import merge3
from paperless_notes.core.session import ConflictInfo, NoteSession, ReloadInfo, Resolution, SessionState
from paperless_notes.ui.theme.tokens import Theme

UNAVAILABLE = "The other version cannot be read as text, so it is not shown."


def changed_lines(base: str, text: str) -> list[int]:
    """0-based lines of ``text`` that are new or different compared with ``base``."""
    model = diff_lines(base, text)
    return [
        line.right_no - 1
        for hunk in model.hunks
        for line in hunk.lines
        if line.op is Op.INSERT and line.right_no is not None
    ]


def starting_result(info: ConflictInfo) -> str:
    """The merge where it is clean, otherwise your text, as the starting point for a manual result."""
    if info.theirs is None:
        return info.ours
    merged = merge3(info.base, info.ours, info.theirs)
    return merged.text if merged.text is not None else info.ours


class ConflictResolver(QDialog):
    def __init__(
        self, session: NoteSession, info: ConflictInfo, theme: Theme, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Changed in two places")
        self.resize(1100, 720)
        self.session = session
        self.info = info
        self.resolution: Resolution | None = None
        self._pending: Resolution | None = None
        self._connections: list[QMetaObject.Connection] = []
        layout = QVBoxLayout(self)
        heading = QLabel("Changed in two places")
        heading.setProperty("role", "heading")
        layout.addWidget(heading)
        intro = QLabel(
            "This note changed on another device or in another app while you were editing it, and the "
            "changes overlap. Nothing has been overwritten. Choose what to keep, or edit the result below."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        grid = QGridLayout()
        self.base = self._pane("Base: the last version both sides had", info.base)
        self.yours = self._pane("Yours: the text in this window", info.ours)
        theirs_text = info.theirs if info.theirs is not None else UNAVAILABLE
        self.theirs = self._pane("Theirs: the version now on disk", theirs_text)
        self.theirs.setEnabled(info.theirs is not None)
        color = QColor(theme.palette.diff_add_background)
        self._mark(self.yours, changed_lines(info.base, info.ours), color)
        if info.theirs is not None:
            self._mark(self.theirs, changed_lines(info.base, info.theirs), color)
        for column, (title, pane) in enumerate(
            (("Base", self.base), ("Yours", self.yours), ("Theirs", self.theirs))
        ):
            label = QLabel(title)
            label.setProperty("role", "heading")
            grid.addWidget(label, 0, column)
            grid.addWidget(pane, 1, column)
        layout.addLayout(grid, 2)
        result_row = QHBoxLayout()
        result_title = QLabel("Result")
        result_title.setProperty("role", "heading")
        result_row.addWidget(result_title)
        self.result_summary = QLabel("")
        self.result_summary.setProperty("role", "secondary")
        result_row.addWidget(self.result_summary, 1)
        for text, source in (
            ("Start from yours", "yours"),
            ("Start from theirs", "theirs"),
            ("Start from both", "both"),
        ):
            button = QPushButton(text)
            button.setProperty("kind", "quiet")
            button.clicked.connect(lambda _c=False, s=source: self.start_from(s))
            button.setEnabled(source == "yours" or info.theirs is not None)
            result_row.addWidget(button)
        layout.addLayout(result_row)
        self.result_edit = QPlainTextEdit(starting_result(info))
        self.result_edit.setObjectName("MergeResult")
        self.result_edit.setAccessibleName("Merged result, editable")
        self.result_edit.textChanged.connect(self._update_summary)
        layout.addWidget(self.result_edit, 2)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        self.message.setProperty("role", "secondary")
        layout.addWidget(self.message)
        both_note = QLabel(
            "Keep both saves your version as a separate copy next to the note and keeps theirs in the note."
        )
        both_note.setWordWrap(True)
        both_note.setProperty("role", "muted")
        layout.addWidget(both_note)
        buttons = QHBoxLayout()
        self.keep_mine = QPushButton("Keep mine")
        self.keep_mine.setToolTip("Save your text over the version on disk; theirs is kept in history")
        self.keep_theirs = QPushButton("Keep theirs")
        self.keep_theirs.setToolTip("Use the version on disk; your text is kept in history")
        self.keep_both = QPushButton("Keep both")
        self.keep_both.setToolTip("Save your version as a separate copy next to the note")
        self.use_result = QPushButton("Use the result")
        self.use_result.setProperty("kind", "primary")
        self.use_result.setToolTip("Save the edited result above")
        cancel = QPushButton("Decide later")
        cancel.setToolTip("Close this window; the note stays in conflict and nothing is saved")
        self.keep_theirs.setEnabled(info.theirs is not None)
        self.keep_both.setEnabled(info.theirs is not None)
        self.keep_mine.clicked.connect(lambda: self.resolve(Resolution.KEEP_MINE))
        self.keep_theirs.clicked.connect(lambda: self.resolve(Resolution.KEEP_THEIRS))
        self.keep_both.clicked.connect(lambda: self.resolve(Resolution.KEEP_BOTH))
        self.use_result.clicked.connect(lambda: self.resolve(Resolution.MANUAL))
        cancel.clicked.connect(self.reject)
        for w in (self.keep_mine, self.keep_theirs, self.keep_both):
            buttons.addWidget(w)
        buttons.addStretch(1)
        buttons.addWidget(cancel)
        buttons.addWidget(self.use_result)
        layout.addLayout(buttons)
        self._update_summary()

    def done(self, result: int) -> None:
        self._disconnect_pending()
        super().done(result)

    @staticmethod
    def _pane(name: str, text: str) -> QPlainTextEdit:
        pane = QPlainTextEdit(text)
        pane.setObjectName("DiffPane")
        pane.setReadOnly(True)
        pane.setAccessibleName(name)
        pane.setToolTip(name)
        return pane

    @staticmethod
    def _mark(pane: QPlainTextEdit, lines: list[int], color: QColor) -> None:
        selections: list[Any] = []
        for number in lines:
            block = pane.document().findBlockByNumber(number)
            if not block.isValid():
                continue
            selection: Any = QTextEdit.ExtraSelection()
            fmt = QTextCharFormat()
            fmt.setBackground(color)
            fmt.setProperty(QTextFormat.Property.FullWidthSelection, True)
            selection.format = fmt
            selection.cursor = QTextCursor(block)
            selections.append(selection)
        pane.setExtraSelections(selections)

    def start_from(self, source: str) -> None:
        theirs = self.info.theirs or ""
        text = {
            "yours": self.info.ours,
            "theirs": theirs,
            "both": self.info.ours.rstrip("\n") + "\n\n" + theirs,
        }[source]
        self.result_edit.setPlainText(text)

    def _update_summary(self) -> None:
        stats = diff_lines(self.info.base, self.result_edit.toPlainText()).stats
        self.result_summary.setText(f"Compared with the base: {stats.added} added, {stats.removed} removed")

    def resolve(self, choice: Resolution) -> bool:
        if self._pending is not None:
            return False
        if self.session.conflict is None:
            self.message.setText("This conflict was already resolved.")
            return False
        self._pending = choice
        self._connections = [
            self.session.saved.connect(self._on_saved),
            self.session.reloaded.connect(self._on_reloaded),
            self.session.problem.connect(self._on_problem),
            self.session.conflict_detected.connect(self._on_new_conflict),
        ]
        self._set_resolution_enabled(False)
        manual = self.result_edit.toPlainText() if choice is Resolution.MANUAL else None
        sibling = self.session.resolve_conflict(choice, manual)
        if self._pending is None:
            return self.resolution is choice
        if self.session.conflict is None and self.session.state is SessionState.READY:
            self._finish_resolution()
            return True
        if choice is Resolution.KEEP_BOTH and sibling is None:
            self._reset_pending()
            self.message.setText("That did not complete. The note is still in conflict; please try again.")
            return False
        self.message.setText(
            "Saving your choice. This comparison stays open until the write finishes safely."
        )
        return True

    def _set_resolution_enabled(self, enabled: bool) -> None:
        self.keep_mine.setEnabled(enabled)
        self.keep_theirs.setEnabled(enabled and self.info.theirs is not None)
        self.keep_both.setEnabled(enabled and self.info.theirs is not None)
        self.use_result.setEnabled(enabled)

    def _disconnect_pending(self) -> None:
        for connection in self._connections:
            QObject.disconnect(connection)
        self._connections.clear()

    def _reset_pending(self) -> None:
        self._disconnect_pending()
        self._pending = None
        self._set_resolution_enabled(True)

    def _finish_resolution(self) -> None:
        choice = self._pending
        if choice is None:
            return
        self._disconnect_pending()
        self._pending = None
        self.resolution = choice
        self.accept()

    def _on_saved(self, *_args: Any) -> None:
        if self._pending in (Resolution.KEEP_MINE, Resolution.MANUAL):
            self._finish_resolution()

    def _on_reloaded(self, info: ReloadInfo) -> None:
        if self._pending is Resolution.KEEP_BOTH and info.kind == "kept_theirs":
            self._finish_resolution()

    def _on_problem(self, code: str, message: str) -> None:
        if self._pending is None:
            return
        self.message.setText(message or "That choice could not be saved yet.")
        if code == "copy_failed":
            self._reset_pending()

    def _on_new_conflict(self, _info: ConflictInfo) -> None:
        if self._pending is None:
            return
        self._disconnect_pending()
        self._pending = None
        self.message.setText(
            "The file changed again while this choice was saving. This comparison remains open; close it "
            "and review the new conflict before choosing again."
        )
