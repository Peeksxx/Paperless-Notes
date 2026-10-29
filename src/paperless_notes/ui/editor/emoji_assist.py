"""Emoji shortcodes while typing, as in chat apps: after ``:`` and two letters a list of matching emoji opens
under the caret. Up and Down move, Tab or Enter inserts the emoji, Escape closes the list, and typing the
closing colon of a known name (``:skull:``) replaces it at once. Nothing opens in code, links or front
matter. Inserting is one undo step."""

from __future__ import annotations

from PySide6.QtCore import QObject, QRect, Qt, QTimer
from PySide6.QtGui import QKeyEvent, QTextCursor
from PySide6.QtWidgets import QHBoxLayout, QListWidget, QListWidgetItem

from paperless_notes.mdio.emoji import completed_at, matches, query_at
from paperless_notes.mdio.lexer import Style
from paperless_notes.mdio.source import in_raw_region, index_of, line_result, position_of
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.popup import Popup, fit_list

NO_EMOJI = Style.CODE | Style.CODE_BLOCK | Style.URL | Style.HTML | Style.META
MAX_ROWS = 8


class EmojiPicker(Popup):
    def __init__(self, editor: NoteEditor) -> None:
        super().__init__(editor, editor.theme)
        self.setAccessibleName("Emoji")
        self.list = QListWidget(self)
        self.list.setProperty("role", "plain")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setAccessibleName("Matching emoji")
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(*self.inner_margins(editor.theme.spacing.xs, editor.theme.spacing.xs))
        layout.addWidget(self.list)

    def labels(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]


class EmojiAssist(QObject):
    """Shows and applies emoji for the shortcode at the caret in one editor view."""

    def __init__(self, editor: NoteEditor) -> None:
        super().__init__(editor)
        self._editor = editor
        self.picker = EmojiPicker(editor)
        self.picker.list.itemClicked.connect(self._insert_item)
        self._start = -1
        editor.key_hooks.insert(0, self._on_key)
        editor.focus_out_hooks.append(self.close)
        editor.theme_changed.connect(self._on_theme)

    def _on_theme(self) -> None:
        self.picker.apply_theme(self._editor.theme)

    def _on_key(self, event: QKeyEvent) -> bool:
        if self.picker.is_open:
            key = event.key()
            rows = self.picker.list
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                step = 1 if key == Qt.Key.Key_Down else -1
                rows.setCurrentRow((rows.currentRow() + step) % rows.count())
                return True
            if key in (Qt.Key.Key_Tab, Qt.Key.Key_Return, Qt.Key.Key_Enter):
                item = rows.currentItem()
                if item is not None:
                    self._insert_item(item)
                    return True
            if key == Qt.Key.Key_Escape:
                self.close()
                return True
        if event.text() or event.key() in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            QTimer.singleShot(0, self, self.update_at_caret)
        return False

    def _allowed(self, column: int) -> bool:
        cursor = self._editor.textCursor()
        block = cursor.block()
        if self._editor.isReadOnly() or in_raw_region(block):
            return False
        spans = line_result(block).spans
        return not any(span.style & NO_EMOJI and span.start < column <= span.end for span in spans)

    def update_at_caret(self) -> None:
        """Open, refresh or close the list for the text before the caret; replace a completed name."""
        cursor = self._editor.textCursor()
        if cursor.hasSelection():
            self.close()
            return
        text = cursor.block().text()
        column = index_of(cursor.block(), cursor.position())
        done = completed_at(text, column)
        if done is not None and self._allowed(column):
            start, emoji = done
            self.close()
            self._replace(position_of(cursor.block(), start), cursor.position(), emoji, space=False)
            return
        found = query_at(text, column)
        if found is None or not self._allowed(column):
            self.close()
            return
        start, query = found
        results = matches(query)
        if not results:
            self.close()
            return
        self._start = position_of(cursor.block(), start)
        rows = self.picker.list
        rows.clear()
        for name, emoji in results:
            item = QListWidgetItem(f"{emoji}   :{name}:")
            item.setData(Qt.ItemDataRole.UserRole, emoji)
            rows.addItem(item)
        rows.setCurrentRow(0)
        self._place()

    def _place(self) -> None:
        editor = self._editor
        fit_list(self.picker.list, editor.rect(), MAX_ROWS)
        caret = editor.cursorRect()
        self.picker.present(QRect(editor.viewport().mapTo(editor, caret.topLeft()), caret.size()))

    def _insert_item(self, item: QListWidgetItem) -> None:
        emoji = str(item.data(Qt.ItemDataRole.UserRole))
        start = self._start
        self.close()
        if start >= 0:
            self._replace(start, self._editor.textCursor().position(), emoji, space=True)

    def _replace(self, start: int, end: int, emoji: str, space: bool) -> None:
        editor = self._editor
        cursor = QTextCursor(editor.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        following = editor.document().characterAt(end)
        add_space = space and following not in " \t.,;:!?)]}"
        cursor.beginEditBlock()
        cursor.insertText(emoji + (" " if add_space else ""))
        cursor.endEditBlock()
        editor.setTextCursor(cursor)

    def close(self) -> None:
        self._start = -1
        self.picker.dismiss()
