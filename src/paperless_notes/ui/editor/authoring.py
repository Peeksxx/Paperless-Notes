"""The one command layer for authoring. Menus, the slash menu, the selection bubble, command search and
keyboard shortcuts all call ``Authoring.execute``; the transformations live in ``mdio.edits`` and
``mdio.tables``. This module adds what a view needs around them: the read-only guard, a plain in-context
message when a command refuses, the selection to show afterwards, restoring the previous selection when
the command is undone, Enter, Tab and slash handling, and paste and drop.
"""

from __future__ import annotations

import logging
import ntpath
from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QGuiApplication, QImage, QImageWriter, QKeyEvent, QKeySequence, QTextCursor

from paperless_notes.core.assets import AssetResult, AssetService
from paperless_notes.core.security.paste import convert_html, has_structure, normalize_pasted_text
from paperless_notes.mdio import edits, tables
from paperless_notes.mdio.edits import EditResult, SlashQuery, done, edit_block, refuse
from paperless_notes.mdio.source import in_raw_region
from paperless_notes.ui.editor.note_editor import NoteEditor

logger = logging.getLogger(__name__)

READ_ONLY = "This note is read-only, so it cannot be changed here."
TAB_SELECTION = "Tab indents list items. Select only list lines, or press Tab without a selection."
DROP_ONLY_IMAGES = "Only images can be dropped into a note. Other files were not added."
MEMO_LIMIT = 100
CONTEXT_CHARS = 200_000


@dataclass(frozen=True)
class AuthoringCommand:
    id: str
    label: str
    purpose: str
    category: str
    run: Callable[[Authoring, QTextCursor], EditResult | None] = field(compare=False)
    shortcut: str = ""
    keywords: tuple[str, ...] = ()
    slash: bool = False
    edits: bool = True
    when: Callable[[Authoring], bool] | None = field(default=None, compare=False)

    @property
    def tooltip(self) -> str:
        return f"{self.label}: {self.purpose}" + (f" ({self.shortcut})" if self.shortcut else "")


@dataclass
class AuthoringHost:
    """Services a command may need beyond the document; all optional so tests can stub them."""

    note_path: Callable[[], str] = field(default=lambda: "")
    assets: AssetService | None = None
    choose_image: Callable[[], str | None] = field(default=lambda: None)
    ask_table_size: Callable[[], tuple[int, int] | None] = field(default=lambda: (3, 2))
    set_clipboard: Callable[[str], None] | None = None
    clipboard_mime: Callable[[], QMimeData | None] | None = None
    toast: Callable[[str], None] = field(default=lambda _text: None)


def _inline(kind: str) -> Callable[[Authoring, QTextCursor], EditResult]:
    return lambda _a, cursor: edits.toggle_inline(cursor, kind)


def _heading(level: int) -> Callable[[Authoring, QTextCursor], EditResult]:
    return lambda _a, cursor: edits.set_heading(cursor, level)


def _list(kind: str) -> Callable[[Authoring, QTextCursor], EditResult]:
    return lambda _a, cursor: edits.set_list(cursor, kind)


def _table_op(op: Callable[[QTextCursor], EditResult]) -> Callable[[Authoring, QTextCursor], EditResult]:
    return lambda _a, cursor: op(cursor)


def _in_table(authoring: Authoring) -> bool:
    return authoring.context_table


def _in_code(authoring: Authoring) -> bool:
    return authoring.context_code


def _insert_table(authoring: Authoring, cursor: QTextCursor) -> EditResult | None:
    size = authoring.host.ask_table_size()
    if size is None:
        return None
    columns, rows = size
    return tables.insert_table(cursor, columns, rows)


def _insert_default_table(_authoring: Authoring, cursor: QTextCursor) -> EditResult:
    return tables.insert_table(cursor)


def _insert_image(authoring: Authoring, cursor: QTextCursor) -> EditResult | None:
    path = authoring.host.choose_image()
    if path is None:
        return None
    return authoring.insert_asset(cursor, lambda service, note: service.store_file(note, path))


def _copy_code(authoring: Authoring, cursor: QTextCursor) -> EditResult:
    text = edits.code_block_text(cursor)
    if text is None:
        return refuse("Put the caret inside a fenced code block to copy its code.")
    if authoring.host.set_clipboard is None:
        return refuse("The clipboard is not available.")
    authoring.host.set_clipboard(text)
    authoring.host.toast("Code copied")
    return done(cursor.anchor(), cursor.position(), False)


def _paste_plain(authoring: Authoring, cursor: QTextCursor) -> EditResult | None:
    source = authoring.host.clipboard_mime() if authoring.host.clipboard_mime is not None else None
    if source is None or not source.hasText():
        return None
    return authoring.insert_plain(cursor, source.text())


COMMANDS: tuple[AuthoringCommand, ...] = (
    AuthoringCommand(
        "bold", "Bold", "wrap the selection in **", "Format", _inline("bold"), "Ctrl+B", ("strong",)
    ),
    AuthoringCommand(
        "italic", "Italic", "wrap the selection in *", "Format", _inline("italic"), "Ctrl+I", ("emphasis",)
    ),
    AuthoringCommand(
        "strike",
        "Strikethrough",
        "wrap the selection in ~~",
        "Format",
        _inline("strike"),
        "Ctrl+Shift+X",
        ("strikeout",),
    ),
    AuthoringCommand(
        "code",
        "Inline code",
        "wrap the selection in backticks",
        "Format",
        _inline("code"),
        "Ctrl+E",
        ("monospace",),
    ),
    AuthoringCommand(
        "link",
        "Link",
        "make the selection a link or edit the link at the caret",
        "Format",
        lambda _a, c: edits.edit_link(c),
        "Ctrl+K",
        ("url", "hyperlink"),
        slash=True,
    ),
    AuthoringCommand(
        "heading1",
        "Heading 1",
        "make the line a top-level heading",
        "Insert",
        _heading(1),
        "Ctrl+Alt+1",
        ("h1", "title"),
        slash=True,
    ),
    AuthoringCommand(
        "heading2",
        "Heading 2",
        "make the line a section heading",
        "Insert",
        _heading(2),
        "Ctrl+Alt+2",
        ("h2",),
        slash=True,
    ),
    AuthoringCommand(
        "heading3",
        "Heading 3",
        "make the line a subsection heading",
        "Insert",
        _heading(3),
        "Ctrl+Alt+3",
        ("h3",),
        slash=True,
    ),
    AuthoringCommand(
        "bullets",
        "Bulleted list",
        "start or remove list bullets",
        "Insert",
        _list("bullet"),
        "",
        ("ul", "unordered"),
        slash=True,
    ),
    AuthoringCommand(
        "numbers",
        "Numbered list",
        "start or remove list numbers",
        "Insert",
        _list("ordered"),
        "",
        ("ol", "ordered"),
        slash=True,
    ),
    AuthoringCommand(
        "tasks",
        "Task list",
        "start or remove task boxes",
        "Insert",
        _list("task"),
        "",
        ("checklist", "todo"),
        slash=True,
    ),
    AuthoringCommand(
        "quote",
        "Quote",
        "start or remove a quote",
        "Insert",
        lambda _a, c: edits.set_quote(c),
        "",
        ("blockquote",),
        slash=True,
    ),
    AuthoringCommand(
        "rule",
        "Horizontal rule",
        "insert a dividing line",
        "Insert",
        lambda _a, c: edits.insert_rule(c),
        "",
        ("divider", "hr", "line"),
        slash=True,
    ),
    AuthoringCommand(
        "codeblock",
        "Code block",
        "insert or fence a code block",
        "Insert",
        lambda _a, c: edits.insert_code_block(c),
        "",
        ("fence", "snippet"),
        slash=True,
    ),
    AuthoringCommand(
        "table", "Table", "insert a 3 by 2 table", "Insert", _insert_default_table, "", ("grid",), slash=True
    ),
    AuthoringCommand(
        "tablesize",
        "Table of a chosen size",
        "choose columns and rows, then insert",
        "Insert",
        _insert_table,
        "",
        ("grid", "columns", "rows"),
    ),
    AuthoringCommand(
        "image",
        "Image",
        "copy an image file next to the note and insert it",
        "Insert",
        _insert_image,
        "",
        ("picture", "photo"),
        slash=True,
    ),
    AuthoringCommand(
        "row_add",
        "Add table row",
        "add an empty row below the caret",
        "Table",
        _table_op(tables.add_row),
        "",
        ("table",),
        when=_in_table,
    ),
    AuthoringCommand(
        "row_remove",
        "Remove table row",
        "remove the caret's row",
        "Table",
        _table_op(tables.remove_row),
        "",
        ("table", "delete"),
        when=_in_table,
    ),
    AuthoringCommand(
        "column_add",
        "Add table column",
        "add an empty column to the right",
        "Table",
        _table_op(tables.add_column),
        "",
        ("table",),
        when=_in_table,
    ),
    AuthoringCommand(
        "column_remove",
        "Remove table column",
        "remove the caret's column",
        "Table",
        _table_op(tables.remove_column),
        "",
        ("table", "delete"),
        when=_in_table,
    ),
    AuthoringCommand(
        "copy_code",
        "Copy code",
        "copy the code inside this block, without its fences",
        "Code",
        _copy_code,
        "",
        ("clipboard",),
        edits=False,
        when=_in_code,
    ),
    AuthoringCommand(
        "line_up",
        "Move lines up",
        "move the selected lines up",
        "Lines",
        lambda _a, c: edits.move_lines(c, True),
        "Alt+Up",
        ("reorder",),
    ),
    AuthoringCommand(
        "line_down",
        "Move lines down",
        "move the selected lines down",
        "Lines",
        lambda _a, c: edits.move_lines(c, False),
        "Alt+Down",
        ("reorder",),
    ),
    AuthoringCommand(
        "line_duplicate",
        "Duplicate lines",
        "copy the selected lines below",
        "Lines",
        lambda _a, c: edits.duplicate_lines(c),
        "Shift+Alt+Down",
        ("copy",),
    ),
    AuthoringCommand(
        "paste_plain",
        "Paste as plain text",
        "paste without converting formatting",
        "Lines",
        _paste_plain,
        "Ctrl+Shift+V",
        ("clipboard",),
    ),
)


class SelectionMemo(QObject):
    """Restores the selection a command started from when that command is undone, and the selection it
    ended with when it is redone. Records are keyed by the undo stack size around the command."""

    restore = Signal(int, int)

    def __init__(self, editor: NoteEditor) -> None:
        super().__init__(editor)
        self._editor = editor
        self._records: list[tuple[int, int, tuple[int, int], tuple[int, int]]] = []
        self._steps = editor.document().availableUndoSteps()
        self._pending = False
        document = editor.document()
        self._connections = [
            document.contentsChanged.connect(self._changed),
            document.undoCommandAdded.connect(self._added),
        ]

    def record(self, steps_before: int, before: tuple[int, int], after: tuple[int, int]) -> None:
        steps_after = self._editor.document().availableUndoSteps()
        if steps_after > steps_before:
            self._records.append((steps_before, steps_after, before, after))
            del self._records[:-MEMO_LIMIT]
        self._steps = steps_after

    def dispose(self) -> None:
        for connection in self._connections:
            QObject.disconnect(connection)
        self._connections.clear()
        self._records.clear()

    def _added(self) -> None:
        steps = self._editor.document().availableUndoSteps()
        self._records = [r for r in self._records if r[1] < steps]

    def _changed(self) -> None:
        if not self._pending:
            self._pending = True
            QTimer.singleShot(0, self, self._check)

    def _check(self) -> None:
        self._pending = False
        steps = self._editor.document().availableUndoSteps()
        previous, self._steps = self._steps, steps
        for before_steps, after_steps, before, after in reversed(self._records):
            if steps == before_steps and previous == after_steps:
                self._select(before)
                return
            if steps == after_steps and previous == before_steps:
                self._select(after)
                return

    def _select(self, selection: tuple[int, int]) -> None:
        document = self._editor.document()
        limit = max(0, document.characterCount() - 1)
        cursor = QTextCursor(document)
        cursor.setPosition(min(selection[0], limit))
        cursor.setPosition(min(selection[1], limit), QTextCursor.MoveMode.KeepAnchor)
        self._editor.setTextCursor(cursor)
        self.restore.emit(selection[0], selection[1])


class Authoring(QObject):
    """Authoring for one editor view. Dispose it before the view goes away."""

    message = Signal(str)
    context_changed = Signal()
    slash_changed = Signal(object)

    def __init__(self, editor: NoteEditor, host: AuthoringHost | None = None) -> None:
        super().__init__(editor)
        self.editor = editor
        self.host = host or AuthoringHost()
        self.commands = {c.id: c for c in COMMANDS}
        self.actions: dict[str, QAction] = {}
        self.context_table = False
        self.context_code = False
        self.slash: SlashQuery | None = None
        self.slash_keys: Callable[[QKeyEvent], bool] | None = None
        self._slash_armed = False
        self.memo = SelectionMemo(editor)
        self._context_timer = QTimer(self)
        self._context_timer.setSingleShot(True)
        self._context_timer.setInterval(60)
        self._context_timer.timeout.connect(self.refresh_context)
        for command in COMMANDS:
            action = QAction(command.label, editor)
            action.setToolTip(command.tooltip)
            action.setStatusTip(command.purpose)
            if command.shortcut:
                action.setShortcut(QKeySequence(command.shortcut))
                action.setShortcutContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            action.triggered.connect(lambda _checked=False, cid=command.id: self.execute(cid))
            editor.addAction(action)
            self.actions[command.id] = action
        self._connections = [
            editor.cursorPositionChanged.connect(self._context_timer.start),
            editor.selectionChanged.connect(self._context_timer.start),
            editor.textChanged.connect(self._on_text_changed),
        ]
        editor.key_hooks.append(self._key)
        editor.focus_out_hooks.append(self.close_slash)
        editor.set_paste_handler(self.paste)
        self.refresh_context()

    def dispose(self) -> None:
        for connection in self._connections:
            QObject.disconnect(connection)
        self._connections.clear()
        self._context_timer.stop()
        if self._key in self.editor.key_hooks:
            self.editor.key_hooks.remove(self._key)
        if self.close_slash in self.editor.focus_out_hooks:
            self.editor.focus_out_hooks.remove(self.close_slash)
        self.editor.set_paste_handler(None)
        for action in self.actions.values():
            self.editor.removeAction(action)
        self.memo.dispose()
        self.close_slash()

    def writable(self) -> bool:
        return not self.editor.isReadOnly()

    def enabled(self, command_id: str) -> bool:
        command = self.commands[command_id]
        if command.edits and not self.writable():
            return False
        return command.when is None or command.when(self)

    def refresh_context(self) -> None:
        """Which contextual commands apply at the caret. Notes too large for live preview skip this on
        every caret move (their block states are unknown, so it would mean lexing); the commands still run
        when chosen."""
        cursor = self.editor.textCursor()
        small = self.editor.document().characterCount() <= CONTEXT_CHARS
        cheap = self.editor.highlighter is not None or small
        self.context_table = cheap and tables.table_at(cursor) is not None
        self.context_code = cheap and edits.code_block_text(cursor, max_lines=2_000) is not None
        for command_id, action in self.actions.items():
            action.setEnabled(self.enabled(command_id))
        self.context_changed.emit()

    def execute(self, command_id: str) -> EditResult:
        """Run one command against the current selection; refusals change nothing and explain why."""
        command = self.commands[command_id]
        if command.edits and not self.writable():
            self.message.emit(READ_ONLY)
            return refuse(READ_ONLY)
        cursor = self.editor.textCursor()
        before = (cursor.anchor(), cursor.position())
        steps = self.editor.document().availableUndoSteps()
        result = command.run(self, QTextCursor(cursor))
        self.close_slash()
        return self.apply(result, before, steps)

    def apply(self, result: EditResult | None, before: tuple[int, int], steps: int) -> EditResult:
        if result is None:
            return refuse("")
        if not result.ok:
            if result.message:
                self.message.emit(result.message)
            return result
        cursor = self.editor.textCursor()
        cursor.setPosition(result.anchor)
        cursor.setPosition(result.position, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        if result.changed:
            self.memo.record(steps, before, (result.anchor, result.position))
        self.message.emit("")
        return result

    def insert_asset(
        self, cursor: QTextCursor, store: Callable[[AssetService, str], AssetResult], alt: str = ""
    ) -> EditResult:
        """Write the asset first; only a successful write inserts the reference (one undo step)."""
        service = self.host.assets
        note = self.host.note_path()
        if service is None or not note:
            return refuse("Images can be added once the note is saved in a folder.")
        problem = image_target_problem(cursor)
        if problem is not None:
            return refuse(problem)
        stored = store(service, note)
        if not stored.ok or stored.reference is None:
            return refuse(stored.problem or "The image could not be added.")
        return edits.insert_image_reference(cursor, stored.reference, alt or asset_alt(stored))

    def insert_plain(self, cursor: QTextCursor, text: str) -> EditResult:
        cleaned = normalize_pasted_text(text)
        document = cursor.document()
        with edit_block(document):
            work = QTextCursor(cursor)
            work.insertText(cleaned)
            end = work.position()
        return done(end)

    def paste(self, source: QMimeData, plain: bool = False) -> bool:
        """Paste or drop. Returns True when handled; images go through the asset service first."""
        if not self.writable():
            self.message.emit(READ_ONLY)
            return True
        cursor = self.editor.textCursor()
        before = (cursor.anchor(), cursor.position())
        steps = self.editor.document().availableUndoSteps()
        result = self._paste_result(QTextCursor(cursor), source, plain)
        if result is None:
            return False
        self.apply(result, before, steps)
        return True

    def _paste_result(self, cursor: QTextCursor, source: QMimeData, plain: bool) -> EditResult | None:
        if plain:
            return self.insert_plain(cursor, source.text()) if source.hasText() else None
        local = [u.toLocalFile() for u in source.urls() if u.isLocalFile()] if source.hasUrls() else []
        if local:
            images = [
                p
                for p in local
                if ntpath.splitext(p)[1].lower() in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
            ]
            if len(images) != len(local):
                return refuse(DROP_ONLY_IMAGES)
            return self._insert_images(cursor, images)
        image_bytes = _image_bytes(source)
        if image_bytes is not None:
            return self.insert_asset(
                cursor, lambda service, note: service.store(note, image_bytes, "pasted-image")
            )
        text = source.text() if source.hasText() else ""
        if cursor.hasSelection() and text and "\n" not in text.strip():
            linked = edits.link_selection_to(cursor, text.strip())
            if linked is not None:
                return linked
        if source.hasHtml() and has_structure(source.html()):
            converted = convert_html(source.html())
            if converted is not None:
                return self.insert_plain(
                    cursor, converted.rstrip("\n") if "\n" not in converted.strip() else converted
                )
        if text:
            return self.insert_plain(cursor, text)
        return None

    def _insert_images(self, cursor: QTextCursor, paths: list[str]) -> EditResult:
        service = self.host.assets
        note = self.host.note_path()
        if service is None or not note:
            return refuse("Images can be added once the note is saved in a folder.")
        problem = image_target_problem(cursor)
        if problem is not None:
            return refuse(problem)
        stored: list[AssetResult] = []
        for path in paths:
            asset = service.store_file(note, path)
            if not asset.ok or asset.reference is None:
                return refuse(asset.problem or "The image could not be added.")
            stored.append(asset)
        document = cursor.document()
        outcome = done(cursor.position())
        with edit_block(document):
            work = QTextCursor(cursor)
            for index, asset in enumerate(stored):
                if index:
                    work = QTextCursor(document)
                    work.setPosition(outcome.position)
                    work.insertText("\n")
                outcome = edits.insert_image_reference(work, asset.reference or "", asset_alt(asset))
                if not outcome.ok:
                    return outcome
        return outcome

    def open_slash(self) -> bool:
        query = edits.slash_query(self.editor.textCursor())
        if query is None or not self.writable():
            return False
        self.slash = query
        self.slash_changed.emit(query)
        return True

    def close_slash(self) -> None:
        self._slash_armed = False
        if self.slash is not None:
            self.slash = None
            self.slash_changed.emit(None)

    def run_slash(self, command_id: str) -> EditResult:
        """Remove the typed ``/query`` and apply the command in the same undo step."""
        query = self.slash
        command = self.commands[command_id]
        if query is None or not command.slash:
            return refuse("")
        if not self.writable():
            self.close_slash()
            self.message.emit(READ_ONLY)
            return refuse(READ_ONLY)
        document = self.editor.document()
        cursor = self.editor.textCursor()
        before = (cursor.anchor(), cursor.position())
        steps = document.availableUndoSteps()
        prepared = self._prepare_slash(command_id)
        if prepared is False:
            self.close_slash()
            return refuse("")
        with edit_block(document):
            edits.remove_range(document, query.start, query.end)
            work = QTextCursor(document)
            work.setPosition(query.start)
            result = prepared(work) if callable(prepared) else command.run(self, work)
            if result is None or not result.ok:
                edit_cursor = QTextCursor(document)
                edit_cursor.setPosition(query.start)
                edit_cursor.insertText("/" + query.text)
        self.close_slash()
        if result is None or not result.ok:
            if result is not None and result.message:
                self.message.emit(result.message)
            return result or refuse("")
        return self.apply(result, before, steps)

    def _prepare_slash(self, command_id: str) -> Callable[[QTextCursor], EditResult] | bool:
        """Work that must succeed before the source changes (choosing and storing an image)."""
        if command_id != "image":
            return True
        path = self.host.choose_image()
        if path is None:
            return False
        service = self.host.assets
        note = self.host.note_path()
        if service is None or not note:
            self.message.emit("Images can be added once the note is saved in a folder.")
            return False
        stored = service.store_file(note, path)
        if not stored.ok or stored.reference is None:
            self.message.emit(stored.problem or "The image could not be added.")
            return False
        reference = stored.reference
        name = asset_alt(stored)
        return lambda work: edits.insert_image_reference(work, reference, name)

    def _on_text_changed(self) -> None:
        if self._slash_armed or self.slash is not None:
            QTimer.singleShot(0, self, self._update_slash)

    def _update_slash(self) -> None:
        if self._slash_armed and self.slash is None:
            self._slash_armed = False
            self.open_slash()
            return
        if self.slash is None:
            return
        query = edits.slash_query(self.editor.textCursor())
        if query is None or query.start != self.slash.start:
            self.close_slash()
            return
        self.slash = query
        self.slash_changed.emit(query)

    def _key(self, event: QKeyEvent) -> bool:
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        if self.slash is not None and self.slash_keys is not None and self.slash_keys(event):
            return True
        if key == Qt.Key.Key_Slash and not modifiers & ~Qt.KeyboardModifier.ShiftModifier:
            self._slash_armed = True
            return False
        if not self.writable():
            return False
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and modifiers == Qt.KeyboardModifier.NoModifier:
            return self._run_key(edits.continue_list)
        if key == Qt.Key.Key_Tab and modifiers == Qt.KeyboardModifier.NoModifier:
            return self._tab(backward=False)
        if key == Qt.Key.Key_Backtab or (
            key == Qt.Key.Key_Tab and modifiers == Qt.KeyboardModifier.ShiftModifier
        ):
            return self._tab(backward=True)
        return False

    def _run_key(self, transform: Callable[[QTextCursor], EditResult | None]) -> bool:
        cursor = self.editor.textCursor()
        before = (cursor.anchor(), cursor.position())
        steps = self.editor.document().availableUndoSteps()
        result = transform(QTextCursor(cursor))
        if result is None:
            return False
        self.apply(result, before, steps)
        return True

    def _tab(self, backward: bool) -> bool:
        if self._run_key(lambda c: tables.next_cell(c, backward)):
            return True
        if self._run_key(lambda c: edits.indent_list(c, backward)):
            return True
        cursor = self.editor.textCursor()
        document = self.editor.document()
        if cursor.hasSelection() and document.findBlock(cursor.selectionStart()) != document.findBlock(
            cursor.selectionEnd()
        ):
            self.message.emit(TAB_SELECTION)
            return True
        return backward


def asset_alt(asset: AssetResult) -> str:
    """Alt text from the stored file name without its content hash."""
    return ntpath.splitext(ntpath.basename(asset.path or "image"))[0].rsplit("-", 1)[0]


def image_target_problem(cursor: QTextCursor) -> str | None:
    """Why an image reference cannot go at the caret, checked before any file is written."""
    document = cursor.document()
    block = document.findBlock(cursor.selectionStart())
    if document.findBlock(cursor.selectionEnd()) != block:
        return edits.ONE_LINE
    if in_raw_region(block):
        return edits.IN_CODE
    return None


def _image_bytes(source: QMimeData) -> bytes | None:
    for fmt in ("image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"):
        if source.hasFormat(fmt):
            data = bytes(source.data(fmt).data())
            if data:
                return data
    if source.hasImage():
        image = source.imageData()
        if isinstance(image, QImage) and not image.isNull():
            buffer = QBuffer()
            buffer.open(QIODevice.OpenModeFlag.WriteOnly)
            if QImageWriter(buffer, QByteArray(b"png")).write(image):
                return bytes(buffer.data().data())
    return None


def qt_decoder(data: bytes, max_pixels: int) -> bool:
    """Full decode check for the asset service, using the same reader the note display uses."""
    from paperless_notes.mdio.document import decode_image

    return decode_image(data, max_pixels) is not None


def system_clipboard_text(text: str) -> None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is not None:
        clipboard.setText(text)


def system_clipboard_mime() -> QMimeData | None:
    clipboard = QGuiApplication.clipboard()
    if clipboard is None:
        return None
    source = clipboard.mimeData()
    if source is None:
        return None
    copy = QMimeData()
    if source.hasText():
        copy.setText(source.text())
    return copy
