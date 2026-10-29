"""Tabs, pages and sessions in up to three tab groups side by side. Each group has its own tabs and shows one
of its notes; the first group also holds Home (shown with no tabs open, or on request while tabs stay open).
Tabs open lazily, persist through WorkspaceStore, and keep recent-order switching, reopen, pins, and back
and forward navigation.

A note open in two groups is two views of one session, made by the same ``PageFactory``: one saver, monitor,
draft stream and ledger, with each view keeping its own caret, selection, scroll and find state. The group
focused last is the active group; new notes, commands, the status bar and search results follow it. A
group after the first closes with its last tab; when the first group's last tab closes while others are
open, the next group's tabs move into it."""

from __future__ import annotations

import logging
import ntpath
from collections.abc import Callable
from functools import partial

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QMenu,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core import pathid
from paperless_notes.core.local_state import LocalStateStore, NoteColors
from paperless_notes.core.manager import SessionManager
from paperless_notes.core.security.filenames import NameCheck
from paperless_notes.core.security.limits import InputLimits
from paperless_notes.core.session import FlushReason, NoteSession, SessionState
from paperless_notes.core.settings import Settings
from paperless_notes.core.workspace import (
    MAX_GROUPS,
    GroupState,
    TabState,
    WindowState,
    Workspace,
    WorkspaceStore,
)
from paperless_notes.ui.editor.authoring import AuthoringHost
from paperless_notes.ui.editor.views import DocumentViews
from paperless_notes.ui.shell.dashboard import Dashboard
from paperless_notes.ui.shell.history import Navigation, TabHistory
from paperless_notes.ui.shell.labels import note_title
from paperless_notes.ui.shell.page import NotePage
from paperless_notes.ui.shell.page_factory import PageFactory
from paperless_notes.ui.shell.tab_group import OpenTab, TabGroup, safe_note_path
from paperless_notes.ui.shell.tabs import NEW_GROUP, TabOverflow, TabStrip
from paperless_notes.ui.shell.transitions import fade_out, rise_in
from paperless_notes.ui.sync.page_sync import SyncHooks
from paperless_notes.ui.theme.tokens import Theme

logger = logging.getLogger(__name__)

_MARKS = {
    SessionState.DIRTY: " \N{BULLET}",
    SessionState.SAVING: " \N{BULLET}",
    SessionState.SAVE_FAILED: " !",
    SessionState.CONFLICT: " !",
    SessionState.MISSING: " ?",
}


class Workbench(QWidget):
    current_changed = Signal(object)
    active_changed = Signal()
    rename_requested = Signal(str, str)
    session_state_changed = Signal()
    home_changed = Signal(bool)
    new_note_requested = Signal()
    groups_changed = Signal()
    copy_path_requested = Signal(str)
    history_requested = Signal()
    note_theme_requested = Signal(object)

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
        export_menu: Callable[[NotePage], QMenu] | None = None,
    ) -> None:
        super().__init__()
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
            export_menu,
        )
        self.history = TabHistory()
        self.navigation = Navigation()
        self._navigating = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.panes = QSplitter(Qt.Orientation.Horizontal)
        self.panes.setChildrenCollapsible(False)
        self.panes.setHandleWidth(1)
        outer.addWidget(self.panes, 1)
        self.start = Dashboard(theme, synced)
        self.start.open_requested.connect(self.open_path)
        self.groups: list[TabGroup] = []
        self._active = self._add_group(self.start)
        app = QApplication.instance()
        self._focus_connection = (
            app.focusChanged.connect(self._on_focus) if isinstance(app, QApplication) else None
        )
        self.apply_theme(theme)
        self._update_empty(self._active)

    @property
    def manager(self) -> SessionManager:
        return self._manager

    @property
    def group(self) -> TabGroup:
        """The active tab group: the one focused last."""
        return self._active

    @property
    def strip(self) -> TabStrip:
        return self._active.strip

    @property
    def stack(self) -> QStackedWidget:
        return self._active.stack

    @property
    def overflow(self) -> TabOverflow:
        return self._active.overflow

    @property
    def new_tab_button(self) -> QToolButton:
        return self._active.new_tab_button

    @property
    def strip_host(self) -> QWidget:
        return self._active.strip_host

    def count(self) -> int:
        return sum(len(g.tabs) for g in self.groups)

    def paths(self) -> list[str]:
        """Every open note once, the first group's first."""
        seen: set[str] = set()
        paths: list[str] = []
        for group in self.groups:
            for path in group.paths():
                key = pathid.identity(path)
                if key not in seen:
                    seen.add(key)
                    paths.append(path)
        return paths

    def pinned_paths(self) -> list[str]:
        return [t.path for g in self.groups for t in g.tabs if t.pinned]

    def index_of(self, path: str) -> int:
        return self._active.index_of(path)

    def current_index(self) -> int:
        return self._active.strip.currentIndex()

    def current_page(self) -> NotePage | None:
        return self._active.current_page()

    def current_session(self) -> NoteSession | None:
        page = self.current_page()
        return page.session if page is not None else None

    def active_page(self) -> NotePage | None:
        """The current note of the active group. None on Home."""
        if self._active.home:
            return None
        return self._active.current_page()

    def group_of_page(self, page: NotePage) -> TabGroup | None:
        return next((g for g in self.groups if any(t.page is page for t in g.tabs)), None)

    def activate_page(self, page: NotePage) -> None:
        """Make the group showing ``page`` the active one (a control on the page was used)."""
        group = self.group_of_page(page)
        if group is not None:
            self._set_active(group)

    def _group_of(self, tab: OpenTab) -> TabGroup | None:
        return next((g for g in self.groups if any(t is tab for t in g.tabs)), None)

    def _add_group(self, start: QWidget | None = None, at: int = -1) -> TabGroup:
        group = TabGroup(self._theme, start)
        strip = group.strip
        strip.currentChanged.connect(lambda i, g=group: self._on_current(i, g))
        strip.tabBarClicked.connect(lambda i, g=group: self._on_tab_clicked(i, g))
        strip.tabMoved.connect(lambda a, b, g=group: self._on_moved(a, b, g))
        strip.close_requested.connect(lambda i, g=group: self.close_tab(i, g))
        strip.pin_toggled.connect(lambda i, g=group: self.toggle_pin(i, g))
        strip.close_others_requested.connect(lambda i, g=group: self.close_others(i, g))
        strip.reopen_requested.connect(lambda g=group: self._reopen_in(g))
        strip.copy_path_requested.connect(lambda i, g=group: self._copy_path(i, g))
        strip.history_requested.connect(lambda i, g=group: self._history(i, g))
        strip.move_requested.connect(lambda i, target, g=group: self.move_tab(g, i, target))
        strip.move_targets = partial(self._move_targets, group)
        group.overflow.activate_requested.connect(lambda i, g=group: self.activate(i, g))
        group.new_tab_button.clicked.connect(lambda _c=False, g=group: self._new_note_in(g))
        group.tab_dropped.connect(lambda key, index, at, g=group: self._dropped(g, key, index, at))
        index = len(self.groups) if at < 0 else min(at, len(self.groups))
        self.groups.insert(index, group)
        self.panes.insertWidget(index, group)
        self._share_width()
        self._update_empty(group)
        return group

    def _share_width(self) -> None:
        count = len(self.groups)
        for index in range(count):
            self.panes.setStretchFactor(index, 1)
        total = max(count, sum(self.panes.sizes()))
        each = total // count
        self.panes.setSizes([each] * (count - 1) + [total - each * (count - 1)])

    def _remove_group(self, group: TabGroup) -> None:
        if group not in self.groups or len(self.groups) < 2:
            return
        for tab in list(group.tabs):
            self._release(tab, group)
        index = self.groups.index(group)
        self.groups.remove(group)
        group.hide()
        group.setParent(None)
        group.deleteLater()
        if self._active is group:
            self._active = self.groups[min(index, len(self.groups) - 1)]
        self._mark()
        self.groups_changed.emit()
        self.active_changed.emit()
        self.current_changed.emit(self.current_page())
        self._save()

    def _set_active(self, group: TabGroup) -> None:
        if group is self._active or group not in self.groups:
            return
        self._active = group
        self._mark()
        self.active_changed.emit()
        self._save()

    def _mark(self) -> None:
        shown = len(self.groups) > 1
        for group in self.groups:
            group.mark(group is self._active, shown)

    def _on_focus(self, _old: QWidget | None, new: QWidget | None) -> None:
        if new is None:
            return
        for group in self.groups:
            if group.isAncestorOf(new):
                self._set_active(group)
                return

    def new_group(self) -> TabGroup | None:
        """Open the active note in a new tab group beside the active one. None on Home or when three groups
        are open already."""
        page = self.active_page()
        if page is None or len(self.groups) >= MAX_GROUPS:
            return None
        group = self._add_group(at=self.groups.index(self._active) + 1)
        self._open_in(group, page.session.path)
        self._mark()
        self.groups_changed.emit()
        new_page = group.current_page()
        if new_page is not None:
            new_page.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        return group

    def close_group(self, group: TabGroup | None = None) -> bool:
        """Close a tab group with its tabs. False when a note that could not be saved keeps it open."""
        group = group or self._active
        if len(self.groups) < 2:
            return False
        while group in self.groups and group.tabs:
            if not self.close_tab(len(group.tabs) - 1, group):
                return False
        return True

    def focus_next_group(self) -> None:
        """Move the keyboard to the next tab group's note (F6), back to the first after the last."""
        if len(self.groups) < 2:
            return
        group = self.groups[(self.groups.index(self._active) + 1) % len(self.groups)]
        self._set_active(group)
        page = None if group.home else group.current_page()
        target: QWidget = page.editor if page is not None else (group.start or group.strip)
        target.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def _move_targets(self, group: TabGroup) -> list[tuple[str, int]]:
        targets = [(f"Move to group {n + 1}", n) for n, g in enumerate(self.groups) if g is not group]
        if len(self.groups) < MAX_GROUPS and len(group.tabs) > 1:
            targets.append(("Move to new group", NEW_GROUP))
        return targets

    def move_tab(self, source: TabGroup, index: int, target: int, at: int = -1) -> None:
        """Move tab ``index`` of ``source`` into group number ``target`` (``NEW_GROUP`` for a new group beside
        ``source``), keeping its page. A note already open in that group is shown there instead."""
        if source not in self.groups or not 0 <= index < len(source.tabs):
            return
        if target == NEW_GROUP:
            if len(self.groups) >= MAX_GROUPS or len(source.tabs) < 2:
                return
            group = self._add_group(at=self.groups.index(source) + 1)
            self.groups_changed.emit()
        elif 0 <= target < len(self.groups):
            group = self.groups[target]
        else:
            return
        if group is source:
            return
        tab = source.tabs[index]
        existing = group.index_of(tab.path)
        if existing >= 0:
            keep = group.tabs[existing]
            if self.close_tab(index, source):
                self._activate_tab(keep)
            return
        moved = self._take(source, index)
        self._put(group, moved, at)
        if not source.tabs:
            self._close_empty(source)
        else:
            self._on_current(source.strip.currentIndex(), source)
        self._activate_tab(moved)
        if moved.page is not None:
            moved.page.editor.setFocus(Qt.FocusReason.OtherFocusReason)
        self._save()

    def _activate_tab(self, tab: OpenTab) -> None:
        group = self._group_of(tab)
        if group is not None:
            self.activate(next(i for i, t in enumerate(group.tabs) if t is tab), group)

    def _dropped(self, target: TabGroup, key: str, index: int, at: int) -> None:
        source = next((g for g in self.groups if g.key == key), None)
        if source is not None and source is not target:
            self.move_tab(source, index, self.groups.index(target), at)

    def _take(self, group: TabGroup, index: int) -> OpenTab:
        """Remove tab ``index`` from ``group``, keeping its page and session for another group."""
        tab = group.tabs.pop(index)
        if tab.page is not None:
            group.stack.removeWidget(tab.page)
        group.strip.blockSignals(True)
        group.strip.removeTab(index)
        group.strip.blockSignals(False)
        return tab

    def _put(self, group: TabGroup, tab: OpenTab, at: int = -1) -> int:
        """Add ``tab`` to ``group`` before ``at`` (-1 for the end), pinned tabs staying in front."""
        pinned = sum(1 for t in group.tabs if t.pinned)
        index = len(group.tabs) if at < 0 else min(at, len(group.tabs))
        index = min(index, pinned) if tab.pinned else max(index, pinned)
        group.tabs.insert(index, tab)
        strip = group.strip
        strip.blockSignals(True)
        strip.insertTab(index, note_title(tab.path))
        strip.set_pinned(index, tab.pinned)
        strip.blockSignals(False)
        if tab.page is not None:
            group.stack.addWidget(tab.page)
        self._refresh_label(tab)
        self._update_empty(group)
        return index

    def _close_empty(self, group: TabGroup) -> None:
        """Take away a group whose last tab closed. The first group holds Home, so when it empties the next
        group's tabs move into it instead."""
        if len(self.groups) < 2 or group.tabs:
            return
        if group is self.groups[0]:
            donor = self.groups[1]
            current = donor.strip.currentIndex()
            while donor.tabs:
                self._put(group, self._take(donor, 0))
            self._remove_group(donor)
            if group.tabs:
                index = max(0, min(current, len(group.tabs) - 1))
                page = group.tabs[index].page
                if page is not None:
                    group.stack.setCurrentWidget(page)
                self.activate(index, group)
            return
        self._remove_group(group)

    def home_visible(self) -> bool:
        return self.groups[0].home_visible()

    def show_home(self) -> None:
        """Show Home in the first group; open tabs stay open, and choosing a tab returns to it."""
        first = self.groups[0]
        if first.tabs and not first.home:
            page = first.current_page()
            if page is not None:
                page.session.flush(FlushReason.TAB_SWITCH)
        before = self._snapshot(first) if not first.home_visible() else None
        first.home = bool(first.tabs)
        first.strip.set_home(first.home)
        first.stack.setCurrentWidget(self.start)
        self.start.set_notes(self.pinned_paths(), self._local.recent_notes())
        if before is not None:
            rise_in(first.stack, before, first.stack.grab(), self.start.section_tops(first.stack))
        if self._active is not first:
            self._active = first
            self._mark()
        self.home_changed.emit(True)
        self.current_changed.emit(None)
        self.active_changed.emit()

    def _snapshot(self, group: TabGroup) -> QPixmap | None:
        """A picture of the group's view for a transition; None when motion is reduced or nothing shows."""
        if self._theme.reduced_motion or not group.stack.isVisible():
            return None
        return group.stack.grab()

    def leave_home(self) -> None:
        first = self.groups[0]
        index = first.strip.currentIndex()
        if first.home and 0 <= index < len(first.tabs):
            self._on_current(index, first)

    def _on_tab_clicked(self, index: int, group: TabGroup | None = None) -> None:
        group = group or self._active
        self._set_active(group)
        if group.home and index == group.strip.currentIndex():
            self._on_current(index, group)

    def pages(self) -> list[NotePage]:
        return [t.page for g in self.groups for t in g.tabs if t.page is not None]

    def open_path(self, path: str, activate: bool = True) -> NoteSession:
        """Show ``path``: in the group that has it open already, or else as a new tab in the active group."""
        if self._active.index_of(path) >= 0:
            group = self._active
        else:
            group = next((g for g in self.groups if g.index_of(path) >= 0), self._active)
        return self._open_in(group, path, activate)

    def _open_in(self, group: TabGroup, path: str, activate: bool = True) -> NoteSession:
        index = group.index_of(path)
        if index < 0:
            index = self._put(group, OpenTab(pathid.normalize(ntpath.abspath(path))))
        tab = group.tabs[index]
        session = self._ensure_open(tab, group)
        if activate:
            self.activate(index, group)
        self._save()
        return session

    def _ensure_open(self, tab: OpenTab, group: TabGroup) -> NoteSession:
        if tab.session is not None:
            return tab.session
        page = self.factory.open(tab.path)
        session = page.session
        self.wire_page(page)
        tab.session = session
        tab.page = page
        group.stack.addWidget(page)
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
        page.theme_requested.connect(lambda p=page: self.note_theme_requested.emit(p))

    def _on_loaded(self, tab: OpenTab) -> None:
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

    def _refresh_label(self, tab: OpenTab) -> None:
        group = self._group_of(tab)
        if group is None:
            return
        index = next(i for i, t in enumerate(group.tabs) if t is tab)
        name = note_title(tab.path)
        state = tab.session.state if tab.session is not None else None
        group.strip.setTabText(index, name + (_MARKS.get(state, "") if state is not None else ""))
        described = state.value.replace("_", " ") if state is not None else "not opened yet"
        group.strip.setTabToolTip(index, f"{tab.path}\n{described}")
        self.session_state_changed.emit()

    def _on_path_changed(self, tab: OpenTab, path: str) -> None:
        self.navigation.rename(tab.path, path)
        tab.path = path
        self._refresh_label(tab)
        self._save()

    def _on_current(self, index: int, group: TabGroup | None = None) -> None:
        group = group or self._active
        for i, tab in enumerate(group.tabs):
            if i != index and tab.session is not None and tab.page is not None and tab.page.isVisible():
                tab.session.flush(FlushReason.TAB_SWITCH)
        was_home = group.home
        before = self._snapshot(group) if group.home_visible() else None
        group.home = False
        group.strip.set_home(False)
        if 0 <= index < len(group.tabs):
            tab = group.tabs[index]
            self._ensure_open(tab, group)
            if tab.page is not None:
                group.stack.setCurrentWidget(tab.page)
                if before is not None:
                    fade_out(group.stack, before, self._theme.ms(self._theme.motion.normal_ms))
            self.history.activated(tab.path)
            if not self._navigating:
                self.navigation.visit(tab.path)
        self._update_empty(group)
        if group is self.groups[0] and (was_home or not group.tabs):
            self.home_changed.emit(self.home_visible())
        self.current_changed.emit(self.current_page())
        self._save()

    def _on_moved(self, source: int, target: int, group: TabGroup | None = None) -> None:
        group = group or self._active
        group.tabs.insert(target, group.tabs.pop(source))
        self._save()

    def _update_empty(self, group: TabGroup) -> None:
        if not group.tabs and group.start is not None:
            group.home = False
            group.stack.setCurrentWidget(group.start)
            self.start.set_notes(self.pinned_paths(), self._local.recent_notes())
        group.strip_host.setVisible(bool(group.tabs))

    def activate(self, index: int, group: TabGroup | None = None) -> None:
        group = group or self._active
        if not 0 <= index < len(group.tabs):
            return
        self._set_active(group)
        if group.strip.currentIndex() == index:
            self._on_current(index, group)
        else:
            group.strip.setCurrentIndex(index)

    def close_tab(self, index: int, group: TabGroup | None = None) -> bool:
        group = group or self._active
        if not 0 <= index < len(group.tabs):
            return False
        tab = group.tabs[index]
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
        self._release(tab, group)
        group.tabs.pop(index)
        group.strip.blockSignals(not group.tabs and len(self.groups) > 1)
        group.strip.removeTab(index)
        group.strip.blockSignals(False)
        if not any(pathid.same_path(tab.path, p) for p in self.paths()):
            self.navigation.forget(tab.path)
        if not group.tabs and len(self.groups) > 1:
            self._close_empty(group)
        else:
            self._update_empty(group)
            if not group.tabs:
                self.home_changed.emit(True)
                self.current_changed.emit(None)
        self._save()
        return True

    def _release(self, tab: OpenTab, group: TabGroup) -> None:
        for connection in tab.connections or []:
            QObject.disconnect(connection)
        tab.connections = None
        if tab.page is not None:
            tab.cursor = tab.page.editor.textCursor().position()
            tab.scroll = tab.page.editor.verticalScrollBar().value()
            group.stack.removeWidget(tab.page)
            self.factory.close(tab.page)
            tab.page.deleteLater()
            tab.page = None
        tab.session = None

    def close_others(self, index: int, group: TabGroup | None = None) -> None:
        group = group or self._active
        keep = group.tabs[index] if 0 <= index < len(group.tabs) else None
        for tab in [t for t in group.tabs if t is not keep and not t.pinned]:
            position = next((i for i, t in enumerate(group.tabs) if t is tab), -1)
            if position >= 0:
                self.close_tab(position, group)

    def reopen_closed(self) -> NoteSession | None:
        state = self.history.reopen()
        if state is None:
            return None
        group = self._active
        index = group.index_of(state.path)
        if index < 0:
            index = self._put(group, OpenTab(state.path, state.pinned, state.cursor, state.scroll))
        session = self._ensure_open(group.tabs[index], group)
        self.activate(index, group)
        return session

    def _reopen_in(self, group: TabGroup) -> None:
        self._set_active(group)
        self.reopen_closed()

    def _new_note_in(self, group: TabGroup) -> None:
        self._set_active(group)
        self.new_note_requested.emit()

    def _copy_path(self, index: int, group: TabGroup) -> None:
        if 0 <= index < len(group.tabs):
            self.copy_path_requested.emit(group.tabs[index].path)

    def _history(self, index: int, group: TabGroup) -> None:
        if 0 <= index < len(group.tabs):
            self.activate(index, group)
            self.history_requested.emit()

    def toggle_pin(self, index: int, group: TabGroup | None = None) -> None:
        group = group or self._active
        if not 0 <= index < len(group.tabs):
            return
        tab = group.tabs[index]
        tab.pinned = not tab.pinned
        group.strip.set_pinned(index, tab.pinned)
        pinned_before = sum(1 for t in group.tabs[:index] if t.pinned)
        if tab.pinned and pinned_before != index:
            group.strip.moveTab(index, pinned_before)
        self._save()

    def switch_recent(self, depth: int = 1) -> None:
        group = self._active
        path = self.history.step(group.paths(), depth)
        if path is not None:
            self.activate(group.index_of(path), group)

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

    def _affected(self, path: str) -> list[tuple[TabGroup, OpenTab]]:
        return [
            (g, t)
            for g in self.groups
            for t in g.tabs
            if pathid.same_path(t.path, path) or pathid.is_within(t.path, path)
        ]

    def release_notes(self, path: str) -> list[str] | None:
        """Save and close the sessions of ``path`` or of notes inside it, keeping their tabs, so the file can
        be renamed or moved. None when a note cannot be saved first; nothing is released then."""
        affected = self._affected(path)
        for _group, tab in affected:
            if tab.session is not None and tab.session.dirty and not tab.session.flush_blocking():
                return None
        for group, tab in affected:
            self._release(tab, group)
        return list(dict.fromkeys(t.path for _g, t in affected))

    def rebind(self, old: str, new: str) -> None:
        """Point tabs at a renamed or moved file or folder and reopen the current ones."""
        self._local.follow_rename(old, new)
        for group in self.groups:
            for tab in group.tabs:
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
        for group in self.groups:
            index = group.strip.currentIndex()
            if 0 <= index < len(group.tabs):
                self._on_current(index, group)
        self._save()

    def close_notes(self, path: str) -> bool:
        """Close every tab of ``path`` or of notes inside it (before deleting it)."""
        while True:
            hit = next(
                (
                    (g, i)
                    for g in self.groups
                    for i, t in enumerate(g.tabs)
                    if pathid.same_path(t.path, path) or pathid.is_within(t.path, path)
                ),
                None,
            )
            if hit is None:
                break
            if not self.close_tab(hit[1], hit[0]):
                return False
        for recent in self._local.recent_notes():
            if pathid.same_path(recent, path) or pathid.is_within(recent, path):
                self._local.forget_recent(recent)
        self._update_empty(self.groups[0])
        return True

    def _state_of(self, tab: OpenTab) -> TabState:
        if tab.page is not None:
            editor = tab.page.editor
            return TabState(
                tab.path, editor.textCursor().position(), editor.verticalScrollBar().value(), tab.pinned
            )
        return TabState(tab.path, tab.cursor, tab.scroll, tab.pinned)

    def current_workspace(self) -> Workspace:
        first, *others = self.groups
        return Workspace(
            tuple(self._state_of(t) for t in first.tabs),
            first.strip.currentIndex(),
            self._window_state(),
            tuple(
                GroupState(tuple(self._state_of(t) for t in g.tabs), g.strip.currentIndex()) for g in others
            ),
            self.groups.index(self._active),
        )

    def _save(self) -> None:
        self._store.schedule(self.current_workspace())

    def restore(self, workspace: Workspace) -> None:
        """Create a tab per saved entry; only each group's active tab opens its note now, the rest when first
        shown. Saved groups after the first keep only ordinary absolute paths to note files."""
        with self._store.suspended():
            first = self.groups[0]
            for state in workspace.tabs:
                if first.index_of(state.path) < 0:
                    self._put(first, OpenTab(state.path, state.pinned, state.cursor, state.scroll))
            showing = [
                workspace.tabs[workspace.active].path if 0 <= workspace.active < len(workspace.tabs) else ""
            ]
            for saved in workspace.groups[: MAX_GROUPS - 1]:
                states = [s for s in saved.tabs if safe_note_path(s.path)]
                if not states:
                    continue
                group = self._add_group()
                for state in states:
                    if group.index_of(state.path) < 0:
                        self._put(group, OpenTab(state.path, state.pinned, state.cursor, state.scroll))
                showing.append(saved.tabs[saved.active].path if 0 <= saved.active < len(saved.tabs) else "")
            for group, path in zip(self.groups, showing, strict=False):
                if group.tabs:
                    self.activate(max(0, group.index_of(path)) if path else 0, group)
            if not first.tabs:
                self._close_empty(first)
            self._set_active(self.groups[min(workspace.active_group, len(self.groups) - 1)])
            self._mark()
            self.groups_changed.emit()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.factory.theme = theme
        for group in self.groups:
            group.apply_theme(theme)
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

    def set_note_colors(self, path: str, colors: NoteColors | None) -> None:
        """Give the note its own (background, text) colours, or the default theme back with None, on every
        page that shows it, and remember them on this PC."""
        self._local.set_note_colors(path, colors)
        kept = self._local.note_colors(path)
        for page in self.pages():
            if pathid.same_path(page.session.path, path):
                page.set_colors(kept)

    def flush_all(self, reason: FlushReason) -> None:
        self._manager.flush_all(reason)

    def dispose(self) -> None:
        if self._focus_connection is not None:
            QObject.disconnect(self._focus_connection)
            self._focus_connection = None
