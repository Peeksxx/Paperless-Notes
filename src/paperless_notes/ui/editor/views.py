"""Every editor view of each open note document (split view).

Two views of one note share one ``QTextDocument`` and so one ``MarkdownHighlighter``: concealment and
heading sizes are formats of the shared document. This registry creates the highlighter once per
document, hands it to every view, and detaches it only when the last view goes, so closing either pane
never leaves the other without styling or with a dangling highlighter. Marker reveal follows the caret
of the last-focused view in every view of the document. Zoom and the note face also belong to the
document (its default font), so the views of one note follow each other.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from PySide6.QtCore import QMetaObject, QObject
from PySide6.QtGui import QTextDocument

from paperless_notes.mdio.highlighter import MarkdownHighlighter
from paperless_notes.ui.editor.note_editor import NoteEditor


@dataclass
class _Group:
    document: QTextDocument
    views: list[NoteEditor] = field(default_factory=list)
    highlighter: MarkdownHighlighter | None = None
    driver: NoteEditor | None = None
    connections: dict[int, list[QMetaObject.Connection]] = field(default_factory=dict)


class DocumentViews(QObject):
    def __init__(self) -> None:
        super().__init__()
        self._groups: list[_Group] = []
        self._syncing = False

    def _group(self, document: QTextDocument) -> _Group | None:
        return next((g for g in self._groups if g.document is document), None)

    def _group_of(self, editor: NoteEditor) -> _Group | None:
        return next((g for g in self._groups if editor in g.views), None)

    def views(self, editor: NoteEditor) -> list[NoteEditor]:
        """Every view of ``editor``'s document, ``editor`` included."""
        group = self._group_of(editor)
        return list(group.views) if group is not None else [editor]

    def view_count(self, document: QTextDocument) -> int:
        group = self._group(document)
        return len(group.views) if group is not None else 0

    def highlighter(self, editor: NoteEditor) -> MarkdownHighlighter | None:
        group = self._group_of(editor)
        return group.highlighter if group is not None else None

    def attach(self, editor: NoteEditor) -> None:
        document = editor.document()
        group = self._group(document)
        if group is None:
            group = _Group(document)
            self._groups.append(group)
        if editor in group.views:
            return
        leader = group.driver
        group.views.append(editor)
        group.connections[id(editor)] = [
            editor.focused.connect(lambda e=editor: self.focus(e)),
            editor.zoom_changed.connect(lambda zoom, e=editor: self._follow_zoom(e, zoom)),
        ]
        if leader is None:
            group.driver = editor
            editor.drives_reveal = True
        else:
            editor.drives_reveal = False
            editor.set_zoom(leader.zoom)
        if group.highlighter is not None:
            editor.set_highlighter(group.highlighter)

    def install_highlighter(self, editor: NoteEditor, create: Callable[[], MarkdownHighlighter]) -> None:
        """Give the document its one highlighter (created on first use) and every view a reference."""
        group = self._group_of(editor)
        if group is None:
            return
        if group.highlighter is None:
            group.highlighter = create()
        for view in group.views:
            if view.highlighter is not group.highlighter:
                view.set_highlighter(group.highlighter)
        if group.driver is not None:
            group.highlighter.set_active_block(group.driver.textCursor().blockNumber())

    def focus(self, editor: NoteEditor) -> None:
        """Make ``editor`` the view whose caret line shows its Markdown markers in every view."""
        group = self._group_of(editor)
        if group is None or group.driver is editor:
            return
        group.driver = editor
        for view in group.views:
            view.drives_reveal = view is editor
        if group.highlighter is not None:
            group.highlighter.set_active_block(editor.textCursor().blockNumber())

    def set_note_font(self, editor: NoteEditor, note_font: str) -> None:
        for view in self.views(editor):
            view.set_note_font(note_font)

    def _follow_zoom(self, editor: NoteEditor, zoom: int) -> None:
        if self._syncing:
            return
        self._syncing = True
        try:
            for view in self.views(editor):
                if view is not editor:
                    view.set_zoom(zoom)
        finally:
            self._syncing = False

    def detach(self, editor: NoteEditor) -> None:
        """Release one view. The highlighter leaves the document only with its last view."""
        group = self._group_of(editor)
        if group is None:
            editor.release_document()
            return
        for connection in group.connections.pop(id(editor), []):
            QObject.disconnect(connection)
        group.views.remove(editor)
        last = not group.views
        if last:
            editor.release_document()
            # A document adopted by two Qt views can stay C++-owned even after both views detach, so
            # dropping every Python reference is not enough to destroy it. The session is released in
            # the same owner-thread turn; queue explicit destruction after that release completes.
            group.document.deleteLater()
        else:
            editor.release_document(detach_highlighter=False)
        editor.drives_reveal = True
        if last:
            self._groups.remove(group)
            return
        if group.driver is editor:
            group.driver = group.views[-1]
            group.driver.drives_reveal = True
            if group.highlighter is not None:
                group.highlighter.set_active_block(group.driver.textCursor().blockNumber())
