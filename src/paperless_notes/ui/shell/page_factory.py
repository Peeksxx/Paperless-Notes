"""Builds and tears down the view of one note for a tab or the split pane.

``open`` acquires the note's one session from the manager (a second view of the same path gets the same
session, adapter and document), creates an editor on that document, registers it with ``DocumentViews``
and wraps it in a ``NotePage``. ``close`` disconnects everything the view added, detaches the editor
(the shared highlighter stays while another view needs it) and releases the view's claim on the
session, which closes only with its last view.
"""

from __future__ import annotations

import ntpath
from collections.abc import Callable

from PySide6.QtCore import QMetaObject, QObject
from PySide6.QtWidgets import QMenu

from paperless_notes.core.local_state import LocalStateStore
from paperless_notes.core.manager import SessionManager
from paperless_notes.core.security.filenames import NameCheck
from paperless_notes.core.security.limits import InputLimits
from paperless_notes.core.security.resources import ResourcePolicy
from paperless_notes.core.session import NoteSession, SessionState
from paperless_notes.core.settings import Settings
from paperless_notes.mdio.adapter import TextDocumentAdapter
from paperless_notes.mdio.document import SafeTextDocument
from paperless_notes.mdio.highlighter import MarkdownHighlighter
from paperless_notes.ui.editor.authoring import AuthoringHost
from paperless_notes.ui.editor.emoji_assist import EmojiAssist
from paperless_notes.ui.editor.note_editor import NoteEditor, highlight_theme
from paperless_notes.ui.editor.spelling_assist import SpellAssist, SpellService
from paperless_notes.ui.editor.views import DocumentViews
from paperless_notes.ui.shell.page import NotePage
from paperless_notes.ui.sync.page_sync import SyncHooks
from paperless_notes.ui.theme.tokens import Theme


class PageFactory:
    def __init__(
        self,
        manager: SessionManager,
        local: LocalStateStore,
        views: DocumentViews,
        theme: Theme,
        hooks: SyncHooks,
        settings: Settings,
        limits: InputLimits,
        opener: Callable[[str], bool] | None,
        validate_title: Callable[[str, str], NameCheck] | None,
        authoring_host: Callable[[NoteSession], AuthoringHost] | None,
        page_menu: Callable[[NotePage], QMenu] | None,
    ) -> None:
        self.manager = manager
        self.views = views
        self.theme = theme
        self.settings = settings
        self._local = local
        self._hooks = hooks
        self._limits = limits
        self._opener = opener
        self._validate_title = validate_title
        self._authoring_host = authoring_host
        self._page_menu = page_menu
        self._connections: dict[int, list[QMetaObject.Connection]] = {}
        self.spelling: SpellService | None = None

    def open(self, path: str) -> NotePage:
        session = self.manager.acquire(path)
        adapter = self.manager.adapter_for(session)
        if not isinstance(adapter, TextDocumentAdapter):
            self.manager.release(session, force=True)
            raise TypeError("note views need a TextDocumentAdapter")
        document = adapter.document
        if isinstance(document, SafeTextDocument):
            document.set_policy(ResourcePolicy(ntpath.dirname(session.path)))
        font = self._local.note_font(session.path) or self.settings.note_font
        editor = NoteEditor(document, self.theme, None, self._opener, font, self.settings.readable_width)
        editor.setReadOnly(adapter.read_only)
        if self.spelling is not None:
            SpellAssist(editor, self.spelling)
        EmojiAssist(editor)
        host = self._authoring_host(session) if self._authoring_host is not None else None
        page = NotePage(
            session,
            editor,
            self.theme,
            self._hooks,
            self._title_check(session),
            host,
            release=self.views.detach,
            menu=self._page_menu,
        )
        self.views.attach(editor)
        self._connections[id(page)] = [
            adapter.read_only_changed.connect(editor.setReadOnly),
            session.loaded.connect(lambda p=page: self.loaded(p)),
        ]
        if session.state is not SessionState.LOADING:
            self.loaded(page)
        return page

    def loaded(self, page: NotePage) -> None:
        """Live preview once the note is loaded and small enough; one highlighter per document."""
        session = page.session
        size = session.disk_stamp.size if session.disk_stamp else 0
        editor = page.editor
        if self.views.highlighter(editor) is None and not self._limits.highlighting_enabled(size):
            return

        def create() -> MarkdownHighlighter:
            highlighter = MarkdownHighlighter(
                editor.document(), highlight_theme(self.theme, "sans", editor.point_size), conceal=True
            )
            if self.spelling is not None:
                self.spelling.attach(highlighter)
            return highlighter

        self.views.install_highlighter(editor, create)

    def close(self, page: NotePage) -> None:
        for connection in self._connections.pop(id(page), []):
            QObject.disconnect(connection)
        page.dispose()
        self.manager.release(page.session, force=True)

    def _title_check(self, session: NoteSession) -> Callable[[str], NameCheck] | None:
        validate = self._validate_title
        if validate is None:
            return None

        def check(name: str) -> NameCheck:
            return validate(session.path, name)

        return check
