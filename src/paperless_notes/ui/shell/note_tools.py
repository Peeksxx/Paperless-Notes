"""Note tools wired to the shell: search and tags (through the search palette), the outline, find and
replace, split view and export. Every tool has a visible route (the search box in the app bar, the outline
panel, the Note menu above each note's text), a command in the palette and, where one exists, a shortcut.
Actions from a pane's own Note menu act on that pane; commands and shortcuts act on the active pane (the
one focused last).
"""

from __future__ import annotations

import ntpath
import time
from collections.abc import Callable

from PySide6.QtCore import QMetaObject, QObject, Qt, QTimer
from PySide6.QtGui import QTextCursor
from PySide6.QtWidgets import QMenu, QWidget

from paperless_notes.core.search_index import SearchIndex
from paperless_notes.core.session import NoteSession, SessionState
from paperless_notes.mdio.find import Finder, FindOptions
from paperless_notes.mdio.outline import Outline, outline_of_document
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.export import Exporter, ExportFormat, ExportResult
from paperless_notes.ui.shell.page import NotePage
from paperless_notes.ui.shell.tool_panel import ToolPanel
from paperless_notes.ui.shell.workbench import Workbench

OUTLINE_DEBOUNCE_MS = 400
RESCAN_INTERVAL_S = 30.0

type CommandSpec = tuple[str, str, Callable[[], object], str, tuple[str, ...]]


class NoteTools(QObject):
    def __init__(
        self,
        workbench: Workbench,
        panel: ToolPanel,
        others: list[QWidget],
        index: SearchIndex | None,
        exporter: Exporter,
        toast: Callable[[str], None],
        open_palette: Callable[[str], None] = lambda _text: None,
    ) -> None:
        super().__init__()
        self.workbench = workbench
        self.panel = panel
        self.index = index
        self.exporter = exporter
        self._others = others
        self._toast = toast
        self._open_palette = open_palette
        self._outline_editor: NoteEditor | None = None
        self._outline_connection: QMetaObject.Connection | None = None
        self._last_rescan = 0.0
        self._outline_timer = QTimer(self)
        self._outline_timer.setSingleShot(True)
        self._outline_timer.setInterval(OUTLINE_DEBOUNCE_MS)
        self._outline_timer.timeout.connect(self.refresh_outline)
        self._connections = [
            panel.outline.heading_chosen.connect(self.reveal_heading),
            workbench.active_changed.connect(self.track_active),
            workbench.current_changed.connect(lambda _page: self.track_active()),
            workbench.manager.session_opened.connect(self._watch),
        ]

    def dispose(self) -> None:
        """Disconnect from the shell before its widgets go away (window close)."""
        self._outline_timer.stop()
        for connection in self._connections:
            QObject.disconnect(connection)
        self._connections.clear()
        if self._outline_connection is not None:
            QObject.disconnect(self._outline_connection)
            self._outline_connection = None
        self._outline_editor = None

    def commands(self) -> list[CommandSpec]:
        return [
            ("search_notes", "Search notes", self.show_search, "Ctrl+Shift+F", ("find", "contents", "text")),
            ("tags", "Browse tags", lambda: self._open_palette("#"), "", ("tag", "hashtag")),
            ("headings", "Go to heading", lambda: self._open_palette("@"), "", ("outline", "jump")),
            ("go_to_line", "Go to line", lambda: self._open_palette(":"), "Ctrl+G", ("number",)),
            ("outline", "Show or hide outline", self.toggle_outline, "Ctrl+Shift+O", ("headings",)),
            (
                "find",
                "Find in note",
                lambda: self._on_active(lambda p: p.find_bar.open_bar(False)),
                "Ctrl+F",
                (),
            ),
            (
                "replace",
                "Replace in note",
                lambda: self._on_active(lambda p: p.find_bar.open_bar(True)),
                "Ctrl+H",
                ("find",),
            ),
            ("find_next", "Find next", lambda: self._on_active(lambda p: p.find_bar.step(False)), "F3", ()),
            (
                "find_previous",
                "Find previous",
                lambda: self._on_active(lambda p: p.find_bar.step(True)),
                "Shift+F3",
                (),
            ),
            ("split_open", "Open split view", self.open_split, "", ("pane", "side by side", "second")),
            ("split_close", "Close split view", self.close_split, "", ("pane", "single")),
            ("switch_pane", "Switch to the other pane", self.workbench.split.focus_other, "F6", ("split",)),
            ("export_html", "Export as HTML", lambda: self._export_active(ExportFormat.HTML), "", ("save",)),
            ("export_pdf", "Export as PDF", lambda: self._export_active(ExportFormat.PDF), "", ("print",)),
            (
                "export_text",
                "Export as plain text",
                lambda: self._export_active(ExportFormat.TEXT),
                "",
                ("txt", "save"),
            ),
            (
                "copy_markdown",
                "Copy as Markdown",
                lambda: self._on_active(self.copy_markdown),
                "",
                ("clipboard",),
            ),
            (
                "copy_rich",
                "Copy as rich text",
                lambda: self._on_active(self.copy_rich),
                "",
                ("clipboard", "html"),
            ),
        ]

    def page_menu(self, page: NotePage) -> QMenu:
        """The Note menu above one note's text; its actions act on that pane."""
        menu = QMenu(page)
        menu.setToolTipsVisible(True)

        def add(text: str, handler: Callable[[], object], tip: str) -> None:
            action = menu.addAction(text)
            action.setToolTip(tip)
            action.triggered.connect(lambda _c=False: self._for(page, handler))

        add("Find in note\tCtrl+F", lambda: page.find_bar.open_bar(False), "Find text in this note")
        add("Replace in note\tCtrl+H", lambda: page.find_bar.open_bar(True), "Find and replace in this note")
        add(
            "Outline\tCtrl+Shift+O",
            lambda: self.show_panel("outline"),
            "Headings of this note in the side panel",
        )
        split = menu.addAction("Split view")
        split.setToolTip("Show a second note side by side, or close the second pane")
        split.triggered.connect(lambda _c=False: self._toggle_split(page))
        menu.aboutToShow.connect(
            lambda: split.setText("Close split view" if self.workbench.split.is_open() else "Open split view")
        )
        menu.addSeparator()
        add("Export as HTML...", lambda: self.export(page, ExportFormat.HTML), "Save a web page of this note")
        add("Export as PDF...", lambda: self.export(page, ExportFormat.PDF), "Save a PDF of this note")
        add(
            "Export as plain text...",
            lambda: self.export(page, ExportFormat.TEXT),
            "Save readable text without Markdown marks",
        )
        add("Copy as Markdown", lambda: self.copy_markdown(page), "Copy the exact Markdown source")
        add("Copy as rich text", lambda: self.copy_rich(page), "Copy formatted text for mail and documents")
        return menu

    def _for(self, page: NotePage, handler: Callable[[], object]) -> None:
        self.workbench.split.activate(page)
        handler()

    def _on_active(self, handler: Callable[[NotePage], object]) -> None:
        page = self.workbench.active_page()
        if page is None:
            self._toast("Open a note first.")
            return
        handler(page)

    def show_search(self) -> None:
        self._open_palette("")

    def show_panel(self, name: str) -> None:
        if name == "tags":
            self._open_palette("#")
            return
        self._hide_others()
        self.panel.show_page(name)
        self.track_active()
        self.refresh_outline()

    def toggle_outline(self) -> None:
        if self.panel.isVisible():
            self.panel.close_panel()
        else:
            self.show_panel("outline")

    def _hide_others(self) -> None:
        for widget in self._others:
            widget.hide()

    def open_split(self) -> None:
        page = self.workbench.active_page()
        self.workbench.split.open(page.session.path if page is not None else None)

    def close_split(self) -> None:
        self.workbench.split.close()

    def _toggle_split(self, page: NotePage) -> None:
        split = self.workbench.split
        if split.is_open():
            split.close()
        else:
            split.open(page.session.path)

    def _open_in_active_pane(self, path: str) -> NotePage | None:
        split = self.workbench.split
        if split.secondary_active():
            return split.show_note(path)
        self.workbench.open_path(path)
        return self.workbench.current_page()

    def open_result(self, path: str, terms: tuple[str, ...]) -> None:
        """Open a search result in the active pane and select its first match; the note is not changed."""
        page = self._open_in_active_pane(path)
        if page is None:
            return
        session = page.session
        if session.state is SessionState.LOADING:
            holder: list[QMetaObject.Connection] = []

            def loaded() -> None:
                for connection in holder:
                    QObject.disconnect(connection)
                reveal_match(page.editor, terms)

            holder.append(session.loaded.connect(loaded))
            return
        reveal_match(page.editor, terms)

    def track_active(self) -> None:
        page = self.workbench.active_page()
        editor = page.editor if page is not None else None
        if editor is self._outline_editor:
            return
        if self._outline_connection is not None:
            QObject.disconnect(self._outline_connection)
            self._outline_connection = None
        self._outline_editor = editor
        if editor is not None:
            self._outline_connection = editor.document().contentsChanged.connect(self._outline_timer.start)
        self.refresh_outline()

    def refresh_outline(self) -> None:
        if self.panel.current() != "outline":
            return
        page = self.workbench.active_page()
        self.panel.outline.show_outline(
            outline_of_document(page.editor.document()) if page is not None else None
        )

    def reveal_heading(self, line: int) -> None:
        page = self.workbench.active_page()
        if page is None:
            return
        editor = page.editor
        block = editor.document().findBlockByNumber(line)
        if not block.isValid():
            return
        editor.setTextCursor(QTextCursor(block))
        editor.centerCursor()
        editor.setFocus(Qt.FocusReason.OtherFocusReason)

    def go_to_line(self, number: int) -> None:
        self.reveal_heading(max(0, number - 1))

    def active_outline(self) -> Outline | None:
        page = self.workbench.active_page()
        return outline_of_document(page.editor.document()) if page is not None else None

    def line_count(self) -> int | None:
        page = self.workbench.active_page()
        return page.editor.document().blockCount() if page is not None else None

    def export(self, page: NotePage, fmt: ExportFormat) -> None:
        session = page.session
        self._toast(f"Exporting {ntpath.basename(session.path)} as {fmt.label}...")
        self.exporter.export(session.path, session.buffer_text(), fmt, self._exported)

    def _export_active(self, fmt: ExportFormat) -> None:
        self._on_active(lambda page: self.export(page, fmt))

    def _exported(self, result: ExportResult) -> None:
        self._toast(result.message)

    def copy_markdown(self, page: NotePage) -> None:
        self._toast(self.exporter.copy_markdown(page.session.buffer_text()))

    def copy_rich(self, page: NotePage) -> None:
        self._toast(self.exporter.copy_rich(page.session.path, page.session.buffer_text()))

    def _watch(self, session: NoteSession) -> None:
        """Saved and reloaded notes are indexed again; a renamed note's folder is checked."""
        index = self.index
        if index is None:
            return
        session.saved.connect(lambda _stamp, s=session: index.refresh([s.path]))
        session.reloaded.connect(lambda _info, s=session: index.refresh([s.path]))
        session.path_changed.connect(lambda path: index.refresh([ntpath.dirname(path)]))

    def files_changed(self, paths: list[str]) -> None:
        if self.index is not None:
            self.index.refresh(paths)

    def roots_changed(self, roots: list[str]) -> None:
        if self.index is not None:
            self.index.set_roots(roots)

    def window_activated(self) -> None:
        """Catch changes other programs made while the window was in the background (throttled)."""
        now = time.monotonic()
        if self.index is not None and now - self._last_rescan >= RESCAN_INTERVAL_S:
            self._last_rescan = now
            self.index.rescan()


def reveal_match(editor: NoteEditor, terms: tuple[str, ...]) -> bool:
    """Select the first place in the note matching the first search term; the text is not changed."""
    editor.setFocus(Qt.FocusReason.OtherFocusReason)
    for term in terms:
        finder = Finder(FindOptions(term))
        result = finder.find_all(editor.document(), max_matches=1)
        if result.ranges:
            start, end = result.ranges[0]
            cursor = QTextCursor(editor.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            editor.setTextCursor(cursor)
            editor.centerCursor()
            return True
    return False
