"""Find and replace in one editor view.

Each view has its own controller and bar, so two panes of one note keep separate searches. Highlights
are extra selections of this view only: they never touch the document, its formats or the undo stack,
and refreshing them after an edit never moves the caret or the selection. Only Next, Previous and the
replace buttons select a match.
"""

from __future__ import annotations

import bisect
from typing import Any

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QKeyEvent, QResizeEvent, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QLabel, QLineEdit, QTextEdit, QToolButton, QVBoxLayout, QWidget

from paperless_notes.mdio.find import Finder, FindOptions, FindResult, replace_all, replace_range
from paperless_notes.ui.editor.authoring import READ_ONLY
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.flow import FlowLayout
from paperless_notes.ui.shell.motion import SlideOut
from paperless_notes.ui.theme.tokens import Theme

REFRESH_MS = 200
TOO_MANY = "Too many matches to replace at once. Narrow the search and try again."


class FindController(QObject):
    """Matches of the current search in one editor, the current match, and replacing."""

    changed = Signal()
    message = Signal(str)

    def __init__(self, editor: NoteEditor, theme: Theme) -> None:
        super().__init__(editor)
        self._editor = editor
        self._theme = theme
        self._finder = Finder(FindOptions())
        self._result = FindResult()
        self._starts: list[int] = []
        self._active = False
        self._painted = -1
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._connections = [
            editor.document().contentsChanged.connect(self._schedule),
            editor.selectionChanged.connect(self._repaint_current),
        ]

    @property
    def options(self) -> FindOptions:
        return self._finder.options

    @property
    def problem(self) -> str:
        return self._finder.problem

    @property
    def result(self) -> FindResult:
        return self._result

    @property
    def active(self) -> bool:
        return self._active

    def set_options(self, options: FindOptions) -> None:
        self._finder = Finder(options)
        self.refresh()

    def activate(self) -> None:
        self._active = True
        self.refresh()

    def deactivate(self) -> None:
        """Stop searching and remove the highlights; the caret and selection stay where they are."""
        self._active = False
        self._timer.stop()
        self._result = FindResult()
        self._starts = []
        self._editor.setExtraSelections([])
        self.changed.emit()

    def set_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._paint()

    def _schedule(self) -> None:
        if self._active:
            self._timer.start()

    def refresh(self) -> None:
        """Find every match again (after typing, an edit or an option change) without moving the caret."""
        self._timer.stop()
        if self._active and self._finder.ready:
            self._result = self._finder.find_all(self._editor.document())
        else:
            self._result = FindResult()
        self._starts = [start for start, _end in self._result.ranges]
        self._paint()
        self.changed.emit()

    def current(self) -> int:
        """Index of the match that is exactly the view's selection, or -1."""
        cursor = self._editor.textCursor()
        start, end = cursor.selectionStart(), cursor.selectionEnd()
        index = bisect.bisect_left(self._starts, start)
        if index < len(self._starts) and self._result.ranges[index] == (start, end):
            return index
        return -1

    def status_text(self) -> str:
        if self._finder.problem:
            return self._finder.problem
        count = len(self._result.ranges)
        if not self._finder.ready:
            return ""
        if count == 0:
            return "No matches"
        total = f"{count:,}" + ("" if self._result.complete else "+")
        index = self.current()
        if index >= 0:
            return f"{index + 1:,} of {total}"
        return f"{total} match{'es' if count != 1 or not self._result.complete else ''}"

    def step(self, backward: bool = False) -> bool:
        """Select the next (or previous) match after the caret, wrapping around; False without matches."""
        ranges = self._result.ranges
        if not ranges:
            return False
        cursor = self._editor.textCursor()
        if backward:
            index = bisect.bisect_left(self._starts, cursor.selectionStart()) - 1
            index = index if index >= 0 else len(ranges) - 1
        else:
            index = bisect.bisect_left(self._starts, cursor.selectionEnd())
            index = index if index < len(ranges) else 0
        self._select(ranges[index])
        return True

    def _select(self, match: tuple[int, int]) -> None:
        cursor = QTextCursor(self._editor.document())
        cursor.setPosition(match[0])
        cursor.setPosition(match[1], QTextCursor.MoveMode.KeepAnchor)
        self._editor.setTextCursor(cursor)
        self._editor.ensureCursorVisible()
        self.changed.emit()

    def replace_one(self, template: str) -> bool:
        """Replace the selected match (one undo step) and select the next one."""
        if self._editor.isReadOnly():
            self.message.emit(READ_ONLY)
            return False
        index = self.current()
        if index < 0:
            return self.step()
        start, end = self._result.ranges[index]
        new_end = replace_range(self._editor.document(), self._finder, start, end, template)
        if new_end is None:
            self.refresh()
            self.message.emit("That match changed; the search was updated.")
            return False
        self.refresh()
        after = bisect.bisect_left(self._starts, new_end)
        if self._result.ranges:
            self._select(self._result.ranges[after if after < len(self._result.ranges) else 0])
        return True

    def replace_all(self, template: str) -> int:
        """Replace every match as one undo step; the view's own selection is kept by Qt's cursor tracking."""
        if self._editor.isReadOnly():
            self.message.emit(READ_ONLY)
            return 0
        count = replace_all(self._editor.document(), self._finder, template)
        if count is None:
            self.message.emit(TOO_MANY)
            return 0
        self.refresh()
        self.message.emit(f"Replaced {count:,} match{'es' if count != 1 else ''}.")
        return count

    def _repaint_current(self) -> None:
        if self._active and self._result.ranges and self.current() != self._painted:
            self._paint()
            self.changed.emit()

    def _paint(self) -> None:
        self._painted = self.current()
        if not self._active or not self._result.ranges:
            self._editor.setExtraSelections([])
            return
        p = self._theme.palette
        other = QTextCharFormat()
        other.setBackground(QColor(p.find_match))
        chosen = QTextCharFormat()
        chosen.setBackground(QColor(p.find_current))
        current = self._painted
        document = self._editor.document()
        selections: list[QTextEdit.ExtraSelection] = []
        for index, (start, end) in enumerate(self._result.ranges):
            selection: Any = QTextEdit.ExtraSelection()
            cursor = QTextCursor(document)
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            selection.cursor = cursor
            selection.format = chosen if index == current else other
            selections.append(selection)
        self._editor.setExtraSelections(selections)

    def dispose(self) -> None:
        self._timer.stop()
        for connection in self._connections:
            QObject.disconnect(connection)
        self._connections.clear()


def _toggle(text: str, name: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setCheckable(True)
    button.setProperty("kind", "labelled")
    button.setAccessibleName(name)
    button.setToolTip(tooltip)
    return button


def _action(text: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setProperty("kind", "labelled")
    button.setAccessibleName(text)
    button.setToolTip(tooltip)
    return button


class _Field(QLineEdit):
    """Enter finds the next match, Shift+Enter the previous one, Escape closes the bar."""

    def __init__(self, bar: FindBar, name: str) -> None:
        super().__init__()
        self._bar = bar
        self.setAccessibleName(name)
        self.setPlaceholderText(name)
        self.setClearButtonEnabled(True)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self is self._bar.replace_field and not event.modifiers():
                self._bar.replace_one()
            else:
                self._bar.step(bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            return
        if key == Qt.Key.Key_Escape:
            self._bar.close_bar()
            return
        if key == Qt.Key.Key_F3:
            self._bar.step(bool(event.modifiers() & Qt.KeyboardModifier.ShiftModifier))
            return
        super().keyPressEvent(event)


class FindBar(QWidget):
    """The find row (field, options, count, previous, next, close) and an optional replace row."""

    closed = Signal()

    def __init__(self, editor: NoteEditor, theme: Theme) -> None:
        super().__init__()
        self.setObjectName("FindBar")
        self.setAccessibleName("Find in this note")
        self.editor = editor
        self.controller = FindController(editor, theme)
        s = theme.spacing
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, s.xs)
        outer.setSpacing(s.xs)
        top = QVBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(s.xs)
        self.find_field = _Field(self, "Find in note")
        self.find_field.setToolTip("Text to find. Enter: next match, Shift+Enter: previous, Escape: close")
        self.case_button = _toggle("Aa", "Match case", "Match upper and lower case exactly")
        self.word_button = _toggle("Word", "Whole word", "Match whole words only")
        self.regex_button = _toggle(".*", "Regular expression", "Treat the text as a regular expression")
        self.count = QLabel("")
        self.count.setProperty("role", "secondary")
        self.count.setAccessibleName("Matches")
        self.count.setMinimumWidth(72)
        self.previous_button = _action("Previous", "Previous match (Shift+F3 or Shift+Enter)")
        self.next_button = _action("Next", "Next match (F3 or Enter)")
        self.close_button = _action("Close", "Close find (Escape)")
        self.close_button.setAccessibleName("Close find")
        top.addWidget(self.find_field)
        options_host = QWidget()
        options = FlowLayout(options_host, spacing=s.xs)
        for widget in (
            self.case_button,
            self.word_button,
            self.regex_button,
            self.count,
            self.previous_button,
            self.next_button,
            self.close_button,
        ):
            options.addWidget(widget)
        top.addWidget(options_host)
        outer.addLayout(top)
        self.replace_row = QWidget()
        bottom = QVBoxLayout(self.replace_row)
        bottom.setContentsMargins(0, 0, 0, 0)
        bottom.setSpacing(s.xs)
        self.replace_field = _Field(self, "Replace with")
        self.replace_field.setToolTip("Replacement text. In regular expression mode $1 inserts a group.")
        self.replace_button = _action("Replace", "Replace this match and go to the next one")
        self.replace_all_button = _action("Replace all", "Replace every match (one undo step)")
        bottom.addWidget(self.replace_field)
        replace_host = QWidget()
        replace_actions = FlowLayout(replace_host, spacing=s.xs)
        replace_actions.addWidget(self.replace_button)
        replace_actions.addWidget(self.replace_all_button)
        bottom.addWidget(replace_host)
        outer.addWidget(self.replace_row)
        self.message = QLabel("")
        self.message.setProperty("role", "secondary")
        self.message.setWordWrap(True)
        self.message.setAccessibleName("Find message")
        outer.addWidget(self.message)
        self.find_field.textChanged.connect(self._options_changed)
        for button in (self.case_button, self.word_button, self.regex_button):
            button.toggled.connect(self._options_changed)
        self.previous_button.clicked.connect(lambda: self.step(True))
        self.next_button.clicked.connect(lambda: self.step(False))
        self.close_button.clicked.connect(self.close_bar)
        self.replace_button.clicked.connect(self.replace_one)
        self.replace_all_button.clicked.connect(self.replace_all)
        self.controller.changed.connect(self._update)
        self.controller.message.connect(self.message.setText)
        self.hide()

    def setVisible(self, visible: bool) -> None:  # noqa: N802 - Qt override
        if not SlideOut.take(self, visible):
            super().setVisible(visible)

    def open_bar(self, replace: bool = False) -> None:
        """Show the bar with the selected text (one line) as the search, and focus the field."""
        cursor = self.editor.textCursor()
        selected = cursor.selectedText()
        if selected and "\N{PARAGRAPH SEPARATOR}" not in selected and len(selected) <= 200:
            self.find_field.setText(selected)
        self.replace_row.setVisible(replace)
        self.message.setText("")
        self.show()
        self.controller.activate()
        self.find_field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.find_field.selectAll()

    def close_bar(self) -> None:
        self.controller.deactivate()
        self.hide()
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self.closed.emit()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        compact = event.size().width() < 220
        self.previous_button.setText("Prev" if compact else "Previous")
        self.replace_all_button.setText("All" if compact else "Replace all")

    def step(self, backward: bool = False) -> None:
        if not self.isVisible():
            self.open_bar()
            return
        if not self.controller.step(backward):
            self._update()

    def replace_one(self) -> None:
        self.controller.replace_one(self.replace_field.text())

    def replace_all(self) -> None:
        self.controller.replace_all(self.replace_field.text())

    def _options_changed(self) -> None:
        self.message.setText("")
        self.controller.set_options(
            FindOptions(
                self.find_field.text(),
                self.case_button.isChecked(),
                self.word_button.isChecked(),
                self.regex_button.isChecked(),
            )
        )

    def _update(self) -> None:
        self.count.setText(self.controller.status_text())
        writable = not self.editor.isReadOnly()
        has = bool(self.controller.result.ranges)
        self.replace_button.setEnabled(writable and has)
        self.replace_all_button.setEnabled(writable and has)
        self.previous_button.setEnabled(has)
        self.next_button.setEnabled(has)

    def apply_theme(self, theme: Theme) -> None:
        self.controller.set_theme(theme)

    def dispose(self) -> None:
        self.controller.dispose()
