"""Tabs, pages and sessions: one tab per note, opened lazily, persisted through WorkspaceStore, with
recent-order switching, reopen, pins, back and forward navigation, the optional second pane, and Home (shown
with no tabs open, or on request while tabs stay open)."""

from __future__ import annotations

import logging
import ntpath
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QMetaObject, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QMenu,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core import pathid
from paperless_notes.core.local_state import LocalStateStore
from paperless_notes.core.manager import SessionManager
from paperless_notes.core.security.filenames import NameCheck
from paperless_notes.core.security.limits import InputLimits
from paperless_notes.core.session import FlushReason, NoteSession, SessionState
from paperless_notes.core.settings import Settings
from paperless_notes.core.workspace import TabState, WindowState, Workspace, WorkspaceStore
from paperless_notes.ui.editor.authoring import AuthoringHost
from paperless_notes.ui.editor.views import DocumentViews
from paperless_notes.ui.shell.dashboard import Dashboard
from paperless_notes.ui.shell.history import Navigation, TabHistory
from paperless_notes.ui.shell.page import NotePage
from paperless_notes.ui.shell.page_factory import PageFactory
from paperless_notes.ui.shell.split import SplitView, safe_split_path
from paperless_notes.ui.shell.tabs import TabOverflow, TabStrip
from paperless_notes.ui.sync.page_sync import SyncHooks
from paperless_notes.ui.theme.icons import glyph_icon
from paperless_notes.ui.theme.tokens import Theme

logger = logging.getLogger(__name__)

_MARKS = {
    SessionState.DIRTY: " \N{BULLET}",
    SessionState.SAVING: " \N{BULLET}",
    SessionState.SAVE_FAILED: " !",
    SessionState.CONFLICT: " !",
    SessionState.MISSING: " ?",
}


@dataclass
class _Tab:
    path: str
    pinned: bool = False
    cursor: int = 0
    scroll: int = 0
    session: NoteSession | None = None
    page: NotePage | None = None
    connections: list[QMetaObject.Connection] | None = None


class Workbench(QWidget):
    current_changed = Signal(object)
    active_changed = Signal()
    rename_requested = Signal(str, str)
    session_state_changed = Signal()
    home_changed = Signal(bool)
    new_note_requested = Signal()

    def __init__(
        self,
        manager: SessionManager,
        store: WorkspaceStore,
        local: LocalStateStore,
        theme: Theme,
        hooks: SyncHooks,
        settings: Settings,
        limits: InputLimits,
        opener: Callable[[str], bool] | None,
        validate_title: Callable[[str, str], NameCheck] | None,
        window_state: Callable[[], WindowState],
        authoring_host: Callable[[NoteSession], AuthoringHost] | None = None,
        page_menu: Callable[[NotePage], QMenu] | None = None,
        synced: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__()
        self._home = False
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Ignored)
        self._manager = manager
        self._store = store
        self._local = local
        self._theme = theme
        self._hooks = hooks
        self._settings = settings
        self._limits = limits
        self._window_state = window_state
        self.views = DocumentViews()
        self.factory = PageFactory(
            manager,
            local,
            self.views,
            theme,
            hooks,
            settings,
            limits,
            opener,
            validate_title,
            authoring_host,
            page_menu,
        )
        self._tabs: list[_Tab] = []
        self.history = TabHistory()
        self.navigation = Navigation()
        self._navigating = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.panes = QSplitter(Qt.Orientation.Horizontal)
        self.panes.setChildrenCollapsible(False)
        self.panes.setHandleWidth(1)
        self.primary = QFrame()
        self.primary.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.primary.setObjectName("PaneFrame")
        self.primary.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.panes.addWidget(self.primary)
        outer.addWidget(self.panes, 1)
        layout = QVBoxLayout(self.primary)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        strip_row = QHBoxLayout()
        strip_row.setContentsMargins(theme.spacing.xs, 0, theme.spacing.xs, 0)
        strip_row.setSpacing(theme.spacing.xxs)
        self.strip = TabStrip(theme)
        self.overflow = TabOverflow(self.strip, theme)
        self.new_tab_button = QToolButton()
        self.new_tab_button.setObjectName("StripButton")
        self.new_tab_button.setAccessibleName("New note")
        self.new_tab_button.setToolTip("New note (Ctrl+N)")
        self.new_tab_button.clicked.connect(self.new_note_requested.emit)
        strip_row.addWidget(self.strip)
        strip_row.addWidget(self.new_tab_button, 0, Qt.AlignmentFlag.AlignVCenter)
        strip_row.addStretch(1)
        strip_row.addWidget(self.overflow, 0, Qt.AlignmentFlag.AlignVCenter)
        self.strip_host = QWidget()
        self.strip_host.setObjectName("TabStripHost")
        self.strip_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.strip_host.setLayout(strip_row)
        layout.addWidget(self.strip_host)
        self.stack = QStackedWidget()
        self.start = Dashboard(theme, synced)
        self.stack.addWidget(self.start)
        layout.addWidget(self.stack, 1)
        self.strip.currentChanged.connect(self._on_current)
        self.strip.tabBarClicked.connect(self._on_tab_clicked)
        self.strip.tabMoved.connect(self._on_moved)
        self.strip.close_requested.connect(self.close_tab)
        self.strip.pin_toggled.connect(self.toggle_pin)
        self.strip.close_others_requested.connect(self.close_others)
        self.strip.reopen_requested.connect(self.reopen_closed)
        self.overflow.activate_requested.connect(self.activate)
        self.start.open_requested.connect(self.open_path)
        self.split = SplitView(
            self.panes, self.primary, self.factory, self._pane_notes, hooks.confirm, self.wire_page
        )
        self.split.focus_primary = self._focus_primary
        self.split.active_changed.connect(self.active_changed.emit)
        self.split.active_changed.connect(
            lambda: self.strip.set_pane_active(not self.split.secondary_active())
        )
        self.split.changed.connect(self._save)
        self.apply_theme(theme)
        self._update_empty()

    @property
    def manager(self) -> SessionManager:
        return self._manager

    def count(self) -> int:
        return len(self._tabs)

    def paths(self) -> list[str]:
        return [t.path for t in self._tabs]

    def pinned_paths(self) -> list[str]:
        return [t.path for t in self._tabs if t.pinned]

    def index_of(self, path: str) -> int:
        key = pathid.identity(path)
        return next((i for i, t in enumerate(self._tabs) if pathid.identity(t.path) == key), -1)

    def current_index(self) -> int:
        return self.strip.currentIndex()

    def current_page(self) -> NotePage | None:
        index = self.strip.currentIndex()
        return self._tabs[index].page if 0 <= index < len(self._tabs) else None

    def current_session(self) -> NoteSession | None:
        page = self.current_page()
        return page.session if page is not None else None

    def active_page(self) -> NotePage | None:
        """The page of the last-focused pane: the second pane's note, or the current tab. None on Home."""
        if self.split.secondary_active():
            return self.split.page
        if self._home:
            return None
        return self.current_page()

    def home_visible(self) -> bool:
        return self.stack.currentWidget() is self.start

    def show_home(self) -> None:
        """Show Home; open tabs stay open, and choosing a tab returns to it."""
        if self._tabs and not self._home:
            page = self.current_page()
            if page is not None:
                page.session.flush(FlushReason.TAB_SWITCH)
        self._home = bool(self._tabs)
        self.strip.set_home(self._home)
        self.stack.setCurrentWidget(self.start)
        self.start.set_notes(self.pinned_paths(), self._local.recent_notes())
        self.home_changed.emit(True)
        self.current_changed.emit(None)
        self.active_changed.emit()

    def leave_home(self) -> None:
        index = self.strip.currentIndex()
        if self._home and 0 <= index < len(self._tabs):
            self._on_current(index)

    def _on_tab_clicked(self, index: int) -> None:
        if self._home and index == self.strip.currentIndex():
            self._on_current(index)

    def pages(self) -> list[NotePage]:
        pages = [t.page for t in self._tabs if t.page is not None]
        return [*pages, self.split.page] if self.split.page is not None else pages

    def _pane_notes(self) -> list[str]:
        return [*self.paths(), *self._local.recent_notes()]

    def _focus_primary(self) -> None:
        page = self.current_page()
        if page is not None:
            page.editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
        else:
            self.start.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def open_path(self, path: str, activate: bool = True) -> NoteSession:
        index = self.index_of(path)
        if index < 0:
            index = self._add(_Tab(pathid.normalize(ntpath.abspath(path))))
        tab = self._tabs[index]
        session = self._ensure_open(tab)
        if activate:
            self.activate(index)
        self._save()
        return session

    def _add(self, tab: _Tab) -> int:
        index = len(self._tabs)
        self._tabs.append(tab)
        self.strip.blockSignals(True)
        self.strip.addTab(ntpath.basename(tab.path))
        self.strip.set_pinned(index, tab.pinned)
        self.strip.blockSignals(False)
        self._refresh_label(tab)
        self._update_empty()
        return index

    def _ensure_open(self, tab: _Tab) -> NoteSession:
        if tab.session is not None:
            return tab.session
        page = self.factory.open(tab.path)
        session = page.session
        self.wire_page(page)
        tab.session = session
        tab.page = page
        self.stack.addWidget(page)
        tab.connections = [
            session.state_changed.connect(lambda _s, t=tab: self._refresh_label(t)),
            session.path_changed.connect(lambda p, t=tab: self._on_path_changed(t, p)),
            session.loaded.connect(lambda t=tab: self._on_loaded(t)),
        ]
        if session.state is not SessionState.LOADING:
            self._on_loaded(tab)
        self._local.add_recent(session.path)
        return session

    def wire_page(self, page: NotePage) -> None:
        """Title renames and sync state changes of any view reach the shell."""
        session = page.session
        page.title.rename_requested.connect(lambda name, s=session: self.rename_requested.emit(s.path, name))
        page.presenter.changed.connect(self.session_state_changed.emit)

    def _on_loaded(self, tab: _Tab) -> None:
        page = tab.page
        session = tab.session
        if page is None or session is None:
            return
        editor = page.editor
        cursor = editor.textCursor()
        cursor.setPosition(min(tab.cursor, max(0, editor.document().characterCount() - 1)))
        editor.setTextCursor(cursor)
        editor.verticalScrollBar().setValue(tab.scroll)
        self._refresh_label(tab)

    def _refresh_label(self, tab: _Tab) -> None:
        index = self._tabs.index(tab) if tab in self._tabs else -1
        if index < 0:
            return
        name = ntpath.basename(tab.path)
        state = tab.session.state if tab.session is not None else None
        self.strip.setTabText(index, name + (_MARKS.get(state, "") if state is not None else ""))
        described = state.value.replace("_", " ") if state is not None else "not opened yet"
        self.strip.setTabToolTip(index, f"{tab.path}\n{described}")
        self.session_state_changed.emit()

    def _on_path_changed(self, tab: _Tab, path: str) -> None:
        self.navigation.rename(tab.path, path)
        tab.path = path
        self._refresh_label(tab)
        self._save()

    def _on_current(self, index: int) -> None:
        for i, tab in enumerate(self._tabs):
            if i != index and tab.session is not None and tab.page is not None and tab.page.isVisible():
                tab.session.flush(FlushReason.TAB_SWITCH)
        was_home = self._home
        self._home = False
        self.strip.set_home(False)
        if 0 <= index < len(self._tabs):
            tab = self._tabs[index]
            self._ensure_open(tab)
            if tab.page is not None:
                self.stack.setCurrentWidget(tab.page)
            self.history.activated(tab.path)
            if not self._navigating:
                self.navigation.visit(tab.path)
        self._update_empty()
        if was_home or not self._tabs:
            self.home_changed.emit(self.home_visible())
        self.current_changed.emit(self.current_page())
        self._save()

    def _on_moved(self, source: int, target: int) -> None:
        self._tabs.insert(target, self._tabs.pop(source))
        self._save()

    def _update_empty(self) -> None:
        if not self._tabs:
            self._home = False
            self.stack.setCurrentWidget(self.start)
            self.start.set_notes(self.pinned_paths(), self._local.recent_notes())
        self.strip_host.setVisible(bool(self._tabs))

    def activate(self, index: int) -> None:
        if not 0 <= index < len(self._tabs):
            return
        if self.strip.currentIndex() == index:
            self._on_current(index)
        else:
            self.strip.setCurrentIndex(index)

    def close_tab(self, index: int) -> bool:
        if not 0 <= index < len(self._tabs):
            return False
        tab = self._tabs[index]
        session = tab.session
        if (
            session is not None
            and self._manager.view_count(session) <= 1
            and session.dirty
            and not session.flush_blocking()
            and not self._hooks.confirm(
                "Not saved",
                f"{ntpath.basename(session.path)} could not be saved. Close the tab anyway? The text is kept "
                "in the recovery journal and offered again when you reopen the note.",
                "Close anyway",
                True,
            )
        ):
            return False
        self.history.removed(self._state_of(tab))
        self._release(tab)
        self._tabs.pop(index)
        self.strip.removeTab(index)
        self.navigation.forget(tab.path)
        self._update_empty()
        if not self._tabs:
            self.home_changed.emit(True)
            self.current_changed.emit(None)
        self._save()
        return True

    def _release(self, tab: _Tab) -> None:
        for connection in tab.connections or []:
            QObject.disconnect(connection)
        tab.connections = None
        if tab.page is not None:
            tab.cursor = tab.page.editor.textCursor().position()
            tab.scroll = tab.page.editor.verticalScrollBar().value()
            self.stack.removeWidget(tab.page)
            self.factory.close(tab.page)
            tab.page.deleteLater()
            tab.page = None
        tab.session = None

    def close_others(self, index: int) -> None:
        keep = self._tabs[index] if 0 <= index < len(self._tabs) else None
        for tab in [t for t in self._tabs if t is not keep and not t.pinned]:
            if tab in self._tabs:
                self.close_tab(self._tabs.index(tab))

    def reopen_closed(self) -> NoteSession | None:
        state = self.history.reopen()
        if state is None:
            return None
        index = self._add(_Tab(state.path, state.pinned, state.cursor, state.scroll))
        session = self._ensure_open(self._tabs[index])
        self.activate(index)
        return session

    def toggle_pin(self, index: int) -> None:
        if not 0 <= index < len(self._tabs):
            return
        tab = self._tabs[index]
        tab.pinned = not tab.pinned
        self.strip.set_pinned(index, tab.pinned)
        pinned_before = sum(1 for t in self._tabs[:index] if t.pinned)
        if tab.pinned and pinned_before != index:
            self.strip.moveTab(index, pinned_before)
        self._save()

    def switch_recent(self, depth: int = 1) -> None:
        path = self.history.step(self.paths(), depth)
        if path is not None:
            self.activate(self.index_of(path))

    def go_back(self) -> None:
        self._go(self.navigation.back())

    def go_forward(self) -> None:
        self._go(self.navigation.forward())

    def _go(self, path: str | None) -> None:
        if path is None:
            return
        self._navigating = True
        try:
            self.open_path(path)
        finally:
            self._navigating = False

    def release_notes(self, path: str) -> list[str] | None:
        """Save and close the sessions of ``path`` or of notes inside it, keeping their tabs, so the file can
        be renamed or moved. None when a note cannot be saved first; nothing is released then."""
        affected = [t for t in self._tabs if pathid.same_path(t.path, path) or pathid.is_within(t.path, path)]
        for tab in affected:
            if tab.session is not None and tab.session.dirty and not tab.session.flush_blocking():
                return None
        if not self.split.release_for(path):
            return None
        for tab in affected:
            self._release(tab)
        return [t.path for t in affected]

    def rebind(self, old: str, new: str) -> None:
        """Point tabs at a renamed or moved file or folder and reopen the current one."""
        for tab in self._tabs:
            if pathid.same_path(tab.path, old):
                replaced = new
            elif pathid.is_within(tab.path, old):
                replaced = new + tab.path[len(old.rstrip("\\")) :]
            else:
                continue
            self.navigation.rename(tab.path, replaced)
            self._local.forget_recent(tab.path)
            tab.path = replaced
            self._refresh_label(tab)
        index = self.strip.currentIndex()
        if 0 <= index < len(self._tabs):
            self._on_current(index)
        self.split.rebind(old, new)
        self._save()

    def close_notes(self, path: str) -> bool:
        """Close every tab of ``path`` or of notes inside it (before deleting it)."""
        for tab in [
            t for t in self._tabs if pathid.same_path(t.path, path) or pathid.is_within(t.path, path)
        ]:
            if tab in self._tabs and not self.close_tab(self._tabs.index(tab)):
                return False
        self.split.forget(path)
        for recent in self._local.recent_notes():
            if pathid.same_path(recent, path) or pathid.is_within(recent, path):
                self._local.forget_recent(recent)
        self._update_empty()
        return True

    def _state_of(self, tab: _Tab) -> TabState:
        if tab.page is not None:
            editor = tab.page.editor
            return TabState(
                tab.path, editor.textCursor().position(), editor.verticalScrollBar().value(), tab.pinned
            )
        return TabState(tab.path, tab.cursor, tab.scroll, tab.pinned)

    def current_workspace(self) -> Workspace:
        split = self.split.state()
        return Workspace(
            tuple(self._state_of(t) for t in self._tabs),
            self.strip.currentIndex(),
            self._window_state(),
            split.open,
            split.path,
        )

    def _save(self) -> None:
        self._store.schedule(self.current_workspace())

    def restore(self, workspace: Workspace) -> None:
        """Create a tab per saved entry; only the active tab opens its note now, the rest when first shown."""
        with self._store.suspended():
            for state in workspace.tabs:
                if self.index_of(state.path) < 0:
                    self._add(_Tab(state.path, state.pinned, state.cursor, state.scroll))
            if self._tabs:
                self.activate(workspace.active if 0 <= workspace.active < len(self._tabs) else 0)
            if workspace.split_open:
                path = workspace.split_path
                self.split.open(path if safe_split_path(path) else None)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.factory.theme = theme
        self.strip.apply_theme(theme)
        self.overflow.apply_theme(theme)
        self.new_tab_button.setIcon(glyph_icon("plus", theme.palette.text_secondary, theme.palette.text))
        self.start.apply_theme(theme)
        for page in self.pages():
            page.apply_theme(theme)

    def apply_settings(self, settings: Settings) -> None:
        self._settings = settings
        self.factory.settings = settings
        for page in self.pages():
            page.editor.set_readable_width(settings.readable_width)
            if self._local.note_font(page.session.path) is None:
                page.editor.set_note_font(settings.note_font)

    def set_note_font(self, font: str) -> None:
        page = self.active_page()
        if page is None:
            return
        self._local.set_note_font(page.session.path, font)
        self.views.set_note_font(page.editor, font)

    def flush_all(self, reason: FlushReason) -> None:
        self._manager.flush_all(reason)
