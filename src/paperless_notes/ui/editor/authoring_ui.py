"""The visible authoring surfaces: the Insert and Format bar above the editor, the slash menu at the caret
and the selection format bubble. They only present ``Authoring`` commands; none edits text itself, and
opening, filtering or closing any of them changes no source and adds no undo step."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QObject, QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QFont, QInputMethodEvent, QKeyEvent, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QSizePolicy,
    QToolButton,
    QWidget,
)

from paperless_notes.mdio.edits import SlashQuery
from paperless_notes.ui.editor.authoring import Authoring, AuthoringCommand
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.flow import FlowLayout
from paperless_notes.ui.theme.tokens import Theme

INSERT_MENU = (
    ("heading1", "heading2", "heading3"),
    ("bullets", "numbers", "tasks", "quote"),
    ("codeblock", "table", "tablesize", "rule"),
    ("image", "link"),
)
FORMAT_MENU = (
    ("bold", "italic", "strike", "code", "link"),
    ("line_up", "line_down", "line_duplicate"),
    ("paste_plain",),
)
TABLE_MENU = (("row_add", "row_remove", "column_add", "column_remove"),)
BUBBLE = (("bold", "B"), ("italic", "I"), ("strike", "S"), ("code", "Code"), ("link", "Link"))
MESSAGE_MS = 7000
MAX_SLASH_ROWS = 8


def build_menu(parent: QWidget, authoring: Authoring, groups: tuple[tuple[str, ...], ...]) -> QMenu:
    menu = QMenu(parent)
    menu.setToolTipsVisible(True)
    for index, group in enumerate(groups):
        if index:
            menu.addSeparator()
        for command_id in group:
            menu.addAction(authoring.actions[command_id])
    return menu


def _menu_button(text: str, tooltip: str, menu: QMenu) -> QToolButton:
    button = QToolButton()
    button.setText(text)
    button.setProperty("kind", "labelled")
    button.setAccessibleName(text)
    button.setToolTip(tooltip)
    button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
    button.setMenu(menu)
    return button


class AuthoringBar(QWidget):
    """Insert and Format menus near the editor, table and code actions when the caret is in one, and a
    short line explaining why a command did nothing."""

    def __init__(self, authoring: Authoring, theme: Theme) -> None:
        super().__init__()
        self.setObjectName("AuthoringBar")
        self.authoring = authoring
        layout = FlowLayout(self, spacing=theme.spacing.xs)
        self.insert_button = _menu_button(
            "Insert",
            "Insert headings, lists, code, tables, images and links (or type / in the text)",
            build_menu(self, authoring, INSERT_MENU),
        )
        self.format_button = _menu_button(
            "Format",
            "Bold, italic, strikethrough, code, links and line commands",
            build_menu(self, authoring, FORMAT_MENU),
        )
        self.table_button = _menu_button(
            "Table",
            "Add or remove rows and columns of the table at the caret",
            build_menu(self, authoring, TABLE_MENU),
        )
        self.copy_button = QToolButton()
        self.copy_button.setDefaultAction(authoring.actions["copy_code"])
        self.copy_button.setProperty("kind", "labelled")
        self.copy_button.setAccessibleName("Copy code")
        self.message = QLabel("")
        self.message.setProperty("role", "secondary")
        self.message.setWordWrap(True)
        self.message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.message.setAccessibleName("Editing message")
        for widget in (self.insert_button, self.format_button, self.table_button, self.copy_button):
            layout.addWidget(widget)
        layout.addWidget(self.message)
        self._clear = QTimer(self)
        self._clear.setSingleShot(True)
        self._clear.timeout.connect(lambda: self.message.setText(""))
        self._connections = [
            authoring.message.connect(self.show_message),
            authoring.context_changed.connect(self.refresh),
        ]
        self.refresh()

    def refresh(self) -> None:
        self.table_button.setVisible(self.authoring.context_table)
        self.copy_button.setVisible(self.authoring.context_code)
        writable = self.authoring.writable()
        self.insert_button.setEnabled(writable)
        self.format_button.setEnabled(True)

    def show_message(self, text: str) -> None:
        self.message.setText(text)
        if text:
            self._clear.start(MESSAGE_MS)

    def dispose(self) -> None:
        for connection in self._connections:
            QObject.disconnect(connection)
        self._connections.clear()


def slash_matches(commands: list[AuthoringCommand], query: str) -> list[AuthoringCommand]:
    """Commands whose label or keywords start with, or contain, the query; prefix matches first."""
    q = query.casefold()
    if not q:
        return commands
    starts: list[AuthoringCommand] = []
    contains: list[AuthoringCommand] = []
    for command in commands:
        words = [
            command.label.casefold(),
            *command.label.casefold().split(),
            *(k.casefold() for k in command.keywords),
        ]
        if any(word.startswith(q) for word in words):
            starts.append(command)
        elif any(q in word for word in words):
            contains.append(command)
    return [*starts, *contains]


class SlashMenu(QFrame):
    """Commands for the ``/query`` at the caret. The editor keeps focus; typing filters, arrows move,
    Enter applies, Escape closes, a click applies."""

    def __init__(self, editor: NoteEditor, authoring: Authoring) -> None:
        super().__init__(editor)
        self.setObjectName("Toast")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAccessibleName("Insert command")
        self.editor = editor
        self.authoring = authoring
        self.list = QListWidget(self)
        self.list.setProperty("role", "plain")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setAccessibleName("Insert commands")
        self.list.itemClicked.connect(self._run_item)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addWidget(self.list)
        self._commands = [c for c in authoring.commands.values() if c.slash]
        authoring.slash_keys = self.handle_key
        self._connection = authoring.slash_changed.connect(self._on_query)
        self.hide()

    def dispose(self) -> None:
        QObject.disconnect(self._connection)
        self.authoring.slash_keys = None
        self.hide()

    def labels(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def _on_query(self, query: SlashQuery | None) -> None:
        if query is None:
            self.hide()
            return
        matches = slash_matches(self._commands, query.text)
        self.list.clear()
        for command in matches:
            item = QListWidgetItem(command.label)
            item.setData(Qt.ItemDataRole.UserRole, command.id)
            item.setToolTip(command.purpose)
            self.list.addItem(item)
        if not matches:
            self.hide()
            return
        self.list.setCurrentRow(0)
        self._place()
        self.show()
        self.raise_()

    def _place(self) -> None:
        rows = min(self.list.count(), MAX_SLASH_ROWS)
        row = max(self.list.sizeHintForRow(0), 24)
        width = max(220, self.list.sizeHintForColumn(0) + 32)
        height = rows * row + 12
        caret = self.editor.cursorRect()
        origin = self.editor.viewport().mapTo(self.editor, caret.bottomLeft())
        area = self.editor.rect()
        x = max(4, min(origin.x(), area.width() - width - 4))
        y = origin.y() + 4
        if y + height > area.height() - 4:
            y = self.editor.viewport().mapTo(self.editor, caret.topLeft()).y() - height - 4
        self.setGeometry(QRect(x, max(4, y), min(width, area.width() - 8), min(height, area.height() - 8)))

    def handle_key(self, event: QKeyEvent) -> bool:
        if not self.isVisible():
            return False
        key = event.key()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down):
            step = -1 if key == Qt.Key.Key_Up else 1
            row = max(0, min(self.list.count() - 1, self.list.currentRow() + step))
            self.list.setCurrentRow(row)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab):
            item = self.list.currentItem()
            if item is not None:
                self._run_item(item)
            return True
        if key == Qt.Key.Key_Escape:
            self.authoring.close_slash()
            return True
        return False

    def _run_item(self, item: QListWidgetItem) -> None:
        self.authoring.run_slash(str(item.data(Qt.ItemDataRole.UserRole)))
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)


class _BubbleButton(QToolButton):
    """Escape returns to the text without the bubble filtering its own children."""

    def __init__(self, parent: QWidget, on_escape: Callable[[], None]) -> None:
        super().__init__(parent)
        self._on_escape = on_escape

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        if event.key() == Qt.Key.Key_Escape:
            self._on_escape()
            return
        super().keyPressEvent(event)


class FormatBubble(QFrame):
    """Bold, italic, strikethrough, code and link for a non-empty selection, placed near it inside the
    viewport. Alt+F10 moves keyboard focus into it and Escape returns to the text. It listens to the
    editor through its hook lists, never through an event filter, so destroying the editor is safe."""

    def __init__(self, editor: NoteEditor, authoring: Authoring) -> None:
        super().__init__(editor)
        self.setObjectName("Hint")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAccessibleName("Format selection")
        self.editor = editor
        self.authoring = authoring
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(2)
        self.buttons: dict[str, QToolButton] = {}
        for command_id, text in BUBBLE:
            button = _BubbleButton(self, self.return_to_text)
            action = authoring.actions[command_id]
            button.setDefaultAction(action)
            button.setText(text)
            button.setAccessibleName(action.text())
            button.setToolTip(action.toolTip())
            button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            font = QFont(button.font())
            font.setBold(command_id == "bold")
            font.setItalic(command_id == "italic")
            font.setStrikeOut(command_id == "strike")
            button.setFont(font)
            layout.addWidget(button)
            self.buttons[command_id] = button
        self._composing = False
        self._shortcut = QShortcut(QKeySequence("Alt+F10"), editor, self.focus_first)
        self._shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
        self._connections = [
            editor.selectionChanged.connect(self.update_state),
            editor.verticalScrollBar().valueChanged.connect(self._reposition),
            editor.horizontalScrollBar().valueChanged.connect(self._reposition),
            editor.column_changed.connect(self._on_resize),
        ]
        editor.key_hooks.append(self._key)
        editor.focus_out_hooks.append(self._focus_out)
        editor.input_method_hooks.append(self._input_method)
        self.hide()

    def dispose(self) -> None:
        for connection in self._connections:
            QObject.disconnect(connection)
        self._connections.clear()
        if self._key in self.editor.key_hooks:
            self.editor.key_hooks.remove(self._key)
        if self._focus_out in self.editor.focus_out_hooks:
            self.editor.focus_out_hooks.remove(self._focus_out)
        if self._input_method in self.editor.input_method_hooks:
            self.editor.input_method_hooks.remove(self._input_method)
        self._shortcut.setEnabled(False)
        self.hide()

    def should_show(self) -> bool:
        cursor = self.editor.textCursor()
        focus = self.editor.hasFocus() or self.isAncestorOf(self.focusWidget() or self)
        return cursor.hasSelection() and focus and self.authoring.writable() and not self._composing

    def update_state(self) -> None:
        if self.should_show():
            self._reposition()
            self.show()
            self.raise_()
        else:
            self.hide()

    def _on_resize(self, _left: int, _width: int) -> None:
        if self.isVisible():
            self._reposition()

    def _reposition(self) -> None:
        if not self.editor.textCursor().hasSelection():
            return
        self.adjustSize()
        cursor = self.editor.textCursor()
        first = self.editor.textCursor()
        first.setPosition(cursor.selectionStart())
        last = self.editor.textCursor()
        last.setPosition(cursor.selectionEnd())
        top = self.editor.cursorRect(first)
        bottom = self.editor.cursorRect(last)
        viewport = self.editor.viewport()
        width, height = self.width(), self.height()
        above = viewport.mapTo(self.editor, QPoint(top.left(), top.top() - height - 6))
        below = viewport.mapTo(self.editor, QPoint(top.left(), bottom.bottom() + 6))
        bounds = viewport.geometry()
        point = above if above.y() >= bounds.top() else below
        x = max(bounds.left() + 4, min(point.x(), bounds.right() - width - 4))
        y = max(bounds.top() + 4, min(point.y(), bounds.bottom() - height - 4))
        self.move(x, y)

    def focus_first(self) -> None:
        if self.isVisible():
            self.buttons["bold"].setFocus(Qt.FocusReason.ShortcutFocusReason)

    def return_to_text(self) -> None:
        self.hide()
        self.editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def _key(self, event: QKeyEvent) -> bool:
        if event.key() == Qt.Key.Key_Escape and self.isVisible():
            self.hide()
            return True
        return False

    def _focus_out(self) -> None:
        QTimer.singleShot(0, self, self.update_state)

    def _input_method(self, event: QInputMethodEvent) -> None:
        self._composing = bool(event.preeditString())
        if self._composing:
            self.hide()
