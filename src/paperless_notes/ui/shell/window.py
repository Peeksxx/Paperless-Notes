"""The main window: composes the app bar, sidebar, workbench, status bar, search palette, help, toasts
and hints, registers every command once, and applies settings. Behaviour lives in the smaller modules it
wires up."""

from __future__ import annotations

import logging
import ntpath
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial

from PySide6.QtCore import QByteArray, QEvent, Qt, QTimer
from PySide6.QtGui import QAction, QCloseEvent, QKeySequence, QShortcut, QShowEvent
from PySide6.QtWidgets import QApplication, QHBoxLayout, QMainWindow, QSplitter, QVBoxLayout, QWidget

from paperless_notes.branding import PRODUCT_NAME
from paperless_notes.core import pathid
from paperless_notes.core.assets import AssetService
from paperless_notes.core.diffing import DiffModel
from paperless_notes.core.evidence import EvidenceStore
from paperless_notes.core.files import FileService
from paperless_notes.core.history import HistoryStore
from paperless_notes.core.instance import OpenRequest, RequestInbox
from paperless_notes.core.local_state import LocalStateStore
from paperless_notes.core.manager import SessionManager
from paperless_notes.core.paths import AppPaths
from paperless_notes.core.runtime import QtIOExecutor
from paperless_notes.core.search_index import DB_NAME, Overview, SearchIndex
from paperless_notes.core.security.launch import note_paths, refused_message
from paperless_notes.core.security.limits import InputLimits
from paperless_notes.core.session import ConflictInfo, FlushReason, NoteSession, SessionConfig, SessionDeps
from paperless_notes.core.settings import Settings, SettingsStore
from paperless_notes.core.watcher import Activity, ChangeMonitor
from paperless_notes.core.workspace import WindowState, WorkspaceStore
from paperless_notes.mdio.adapter import TextDocumentAdapter
from paperless_notes.mdio.document import SafeTextDocument
from paperless_notes.mdio.tags import extract_tags
from paperless_notes.ui.editor.authoring import (
    AuthoringHost,
    qt_decoder,
    system_clipboard_mime,
    system_clipboard_text,
)
from paperless_notes.ui.shell.chrome import (
    AppBar,
    EdgeResizer,
    MaximizeHitTest,
    PopupRounder,
    allow_snap_layouts,
    request_native_decoration,
)
from paperless_notes.ui.shell.commands import Command, CommandRegistry
from paperless_notes.ui.shell.dialogs import DialogPrompter, Prompter
from paperless_notes.ui.shell.export import Clipboard, Exporter, system_clipboard
from paperless_notes.ui.shell.help import HelpPanel, HintBubble, pending_hints
from paperless_notes.ui.shell.labels import note_title
from paperless_notes.ui.shell.library_actions import LibraryActions
from paperless_notes.ui.shell.note_tools import NoteTools
from paperless_notes.ui.shell.palette import PaletteSources, SearchPalette
from paperless_notes.ui.shell.settings_panel import SettingsDialog
from paperless_notes.ui.shell.sidebar import Sidebar
from paperless_notes.ui.shell.statusbar import StatusBar
from paperless_notes.ui.shell.toast import Toast
from paperless_notes.ui.shell.tool_panel import OutlinePage, ToolPanel
from paperless_notes.ui.shell.workbench import Workbench
from paperless_notes.ui.sync.conflict import ConflictResolver
from paperless_notes.ui.sync.diff_view import DiffDialog
from paperless_notes.ui.sync.history import HistoryDialog
from paperless_notes.ui.sync.page_sync import SyncHooks, session_tone, summarize
from paperless_notes.ui.theme.manager import ThemeManager
from paperless_notes.ui.theme.style import ShellStyle
from paperless_notes.ui.theme.tokens import LIGHT, Theme, theme

logger = logging.getLogger(__name__)

WEEK_S = 7 * 86400


@dataclass
class ShellServices:
    settings: Settings
    settings_store: SettingsStore | None = None
    theme_manager: ThemeManager | None = None
    files: FileService | None = None
    history: HistoryStore | None = None
    evidence: EvidenceStore | None = None
    prompter: Prompter | None = None
    opener: Callable[[str], bool] | None = None
    assets: AssetService | None = None
    search_index: SearchIndex | None = None
    clipboard: Clipboard | None = None


class MainWindow(QMainWindow):
    def __init__(
        self,
        paths: AppPaths,
        deps: SessionDeps,
        config: SessionConfig,
        monitor: ChangeMonitor,
        limits: InputLimits | None = None,
        services: ShellServices | None = None,
    ) -> None:
        super().__init__()
        self._maximize_hit: MaximizeHitTest | None = None
        self.services = services or ShellServices(Settings())
        self.settings = self.services.settings
        self._paths = paths
        self._monitor = monitor
        self._native_frame = not self.settings.custom_frame
        self._themes = self.services.theme_manager
        self.theme: Theme = self._themes.theme if self._themes is not None else theme(LIGHT)
        self.prompter: Prompter = self.services.prompter or DialogPrompter(self)
        self._local = LocalStateStore(paths.state_file)
        self._manager = SessionManager(deps, config, self._new_adapter, monitor, self._local)
        self._store = WorkspaceStore(paths.session_file, deps.scheduler)
        self._files = self.services.files or FileService(deps.fs, deps.onedrive_roots)
        self._assets = self.services.assets or AssetService(deps.fs, qt_decoder)
        self._deps = deps
        self._index_executor: QtIOExecutor | None = None
        self.index = self.services.search_index or self._make_index(paths, deps)
        self._dialogs: list[QWidget] = []
        self._hints_started = False
        self.setWindowTitle(PRODUCT_NAME)
        self.resize(1365, 900)
        self.setMinimumSize(720, 480)
        if not self._native_frame:
            self.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        self._build(limits or InputLimits())
        self._register_commands()
        self._inbox = RequestInbox(paths.ipc_dir)
        self._inbox.received.connect(self._on_request)
        if self._themes is not None:
            self._themes.changed.connect(self.apply_theme)
        self._show_current(None)
        self.index.start(list(self.settings.library_roots))
        self.refresh_home()

    def _make_index(self, paths: AppPaths, deps: SessionDeps) -> SearchIndex:
        """The note search index on its own one-thread worker, in the local state folder."""
        self._index_executor = QtIOExecutor(threads=1)
        return SearchIndex(paths.index_dir / DB_NAME, self._index_executor, deps.fs, extract_tags)

    def synced(self, path: str) -> bool:
        """True when ``path`` is inside a OneDrive folder."""
        return any(
            pathid.is_within(path, root) or pathid.same_path(path, root) for root in self._deps.onedrive_roots
        )

    def _build(self, limits: InputLimits) -> None:
        t = self.theme
        root = QWidget()
        root.setObjectName("AppRoot")
        root.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._root = root
        outer = QVBoxLayout(root)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        self.bar = AppBar(self, t, self._native_frame)
        self.bar.minimize_requested.connect(self.showMinimized)
        self.bar.maximize_toggled.connect(self.toggle_maximized)
        self.bar.close_requested.connect(self.close)
        outer.addWidget(self.bar)
        if not self._native_frame:
            self._maximize_hit = MaximizeHitTest(self, self.bar.maximize_button)
        self.splitter = QSplitter(Qt.Orientation.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(1)
        self.sidebar = Sidebar(t, synced=self.synced)
        self.sidebar.setMaximumWidth(t.metrics.sidebar_max)
        self.sidebar.set_roots(list(self.settings.library_roots))
        self.splitter.addWidget(self.sidebar)
        main = QWidget()
        main_layout = QHBoxLayout(main)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)
        self.hooks = SyncHooks(self.toast_message, self.open_diff, self.open_conflict, self._confirm)
        self.workbench = Workbench(
            self._manager,
            self._store,
            self._local,
            t,
            self.hooks,
            self.settings,
            limits,
            self.services.opener,
            self._files.check_rename,
            self._window_state,
            self._authoring_host,
            lambda page: self.tools.page_menu(page),
            self.synced,
        )
        main_layout.addWidget(self.workbench, 1)
        self.tool_panel = ToolPanel(t, OutlinePage(t))
        main_layout.addWidget(self.tool_panel)
        self.help_panel = HelpPanel(t)
        main_layout.addWidget(self.help_panel)
        self.splitter.addWidget(main)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([t.metrics.sidebar, 1000])
        outer.addWidget(self.splitter, 1)
        self.status = StatusBar(t)
        outer.addWidget(self.status)
        self.setCentralWidget(root)
        self.toast = Toast(root, t)
        self.hint = HintBubble(root, t)
        self.hint.dismissed.connect(self._dismiss_hint)
        self.hint.hide_all.connect(self._hide_hints)
        self.library = LibraryActions(
            self._files, self.prompter, self.workbench, self.sidebar, self.toast_message, self._hwnd
        )
        self.library.roots_changed.connect(self._save_roots)
        self.exporter = Exporter(
            self._deps.fs,
            self._assets,
            self.prompter,
            self._deps.executor,
            lambda: self.theme,
            self._open_note_paths,
            self.services.clipboard or system_clipboard,
        )
        self.tools = NoteTools(
            self.workbench,
            self.tool_panel,
            [self.help_panel],
            self.index,
            self.exporter,
            self.toast_message,
            self.open_palette,
        )
        self.library.files_changed.connect(self.tools.files_changed)
        self.registry = CommandRegistry()
        self.search_palette = SearchPalette(
            root,
            t,
            PaletteSources(
                self.registry,
                self.index,
                self.workbench.paths,
                self._local.recent_notes,
                self.sidebar.roots,
                self.tools.open_result,
                self.tools.active_outline,
                self.tools.reveal_heading,
                self.tools.line_count,
                self.tools.go_to_line,
            ),
            lambda: self.bar.geometry().bottom(),
        )
        self.bar.search_requested.connect(lambda: self.open_palette(""))
        self.bar.home_requested.connect(self.show_home)
        self.bar.sidebar_toggled.connect(self.toggle_sidebar)
        self.bar.back_requested.connect(self.workbench.go_back)
        self.bar.forward_requested.connect(self.workbench.go_forward)
        self.bar.history_requested.connect(self.open_history)
        self.bar.help_requested.connect(self.toggle_help)
        self.bar.settings_requested.connect(self.open_settings)
        self.bar.crumbs.folder_requested.connect(self._reveal_folder)
        self.sidebar.home_requested.connect(self.show_home)
        self.sidebar.search_requested.connect(lambda: self.open_palette(""))
        self.workbench.active_changed.connect(lambda: self._show_current(None))
        self.workbench.split.choose_file = self._choose_note_file
        self.workbench.current_changed.connect(self._show_current)
        self.workbench.session_state_changed.connect(self._sessions_changed)
        self.workbench.home_changed.connect(self._home_changed)
        self.workbench.new_note_requested.connect(lambda: self._discard(self.library.new_note()))
        self.workbench.strip.history_requested.connect(self._history_for_tab)
        self.workbench.strip.copy_path_requested.connect(self._copy_tab_path)
        home = self.workbench.start
        home.new_note_requested.connect(self.library.new_note)
        home.open_file_requested.connect(self.open_dialog)
        home.search_requested.connect(self.open_palette)
        home.folder_requested.connect(self._reveal_folder)
        home.set_roots(list(self.settings.library_roots))
        self.index.content_changed.connect(self.refresh_home)
        self._manager.session_closed.connect(self._on_session_closed)
        self.status.zoom_reset_requested.connect(lambda: self._with_editor(lambda e: e.set_zoom(100)))
        self._rounder = PopupRounder(t, self)
        if not self._native_frame:
            self._resizer = EdgeResizer(self)

    @property
    def manager(self) -> SessionManager:
        return self._manager

    def _new_adapter(self) -> TextDocumentAdapter:
        return TextDocumentAdapter(SafeTextDocument())

    def _hwnd(self) -> int | None:
        return int(self.winId()) if self.isVisible() else None

    def _register_commands(self) -> None:
        wb = self.workbench
        items: list[tuple[str, str, Callable[[], object], str, tuple[str, ...]]] = [
            ("new_note", "New note", lambda: self._discard(self.library.new_note()), "Ctrl+N", ("create",)),
            ("new_folder", "New folder", lambda: self._discard(self.library.new_folder()), "", ("create",)),
            ("add_folder", "Add folder to library", lambda: self._discard(self.library.add_root()), "", ()),
            ("open_file", "Open file", self.open_dialog, "Ctrl+O", ("browse",)),
            ("save", "Save now", self.save_current, "Ctrl+S", ()),
            ("close_tab", "Close tab", lambda: self._discard(wb.close_tab(wb.current_index())), "Ctrl+W", ()),
            (
                "reopen_tab",
                "Reopen closed tab",
                lambda: self._discard(wb.reopen_closed()),
                "Ctrl+Shift+T",
                (),
            ),
            ("recent_tab", "Switch to recent tab", lambda: wb.switch_recent(1), "Ctrl+Tab", ("mru",)),
            (
                "recent_tab_back",
                "Switch to recent tab, other way",
                lambda: wb.switch_recent(-1),
                "Ctrl+Shift+Tab",
                (),
            ),
            ("pin_tab", "Pin or unpin tab", lambda: wb.toggle_pin(wb.current_index()), "", ()),
            ("back", "Go back", wb.go_back, "Alt+Left", ("navigate",)),
            ("forward", "Go forward", wb.go_forward, "Alt+Right", ("navigate",)),
            ("home", "Go home", self.show_home, "Alt+Home", ("dashboard", "start", "recent")),
            ("sidebar", "Show or hide sidebar", self.toggle_sidebar, "Ctrl+\\", ("library",)),
            ("search", "Search", lambda: self.open_palette(""), "Ctrl+Shift+P", ("palette", "find", "notes")),
            ("commands", "Show all commands", self.focus_commands, "", ("palette", "actions")),
            ("help", "Help", self.toggle_help, "F1", ("help", "guide", "keyboard", "shortcuts")),
            ("settings", "Settings", self.open_settings, "Ctrl+,", ("preferences", "options")),
            ("history", "Version history", self.open_history, "Ctrl+Shift+H", ("restore", "versions")),
            (
                "theme_system",
                "Theme: Follow Windows",
                lambda: self.set_theme_mode("system"),
                "",
                ("appearance",),
            ),
            ("theme_light", "Theme: Light", lambda: self.set_theme_mode("light"), "", ("appearance",)),
            ("theme_dark", "Theme: Dark", lambda: self.set_theme_mode("dark"), "", ("appearance",)),
            ("font_sans", "Note font: Sans", lambda: wb.set_note_font("sans"), "", ("typeface",)),
            ("font_serif", "Note font: Serif", lambda: wb.set_note_font("serif"), "", ("typeface",)),
            ("font_mono", "Note font: Mono", lambda: wb.set_note_font("mono"), "", ("typeface",)),
            ("zoom_in", "Zoom in", lambda: self._with_editor(lambda e: e.zoom_in()), "Ctrl+=", ()),
            ("zoom_out", "Zoom out", lambda: self._with_editor(lambda e: e.zoom_out()), "Ctrl+-", ()),
            ("zoom_reset", "Reset zoom", lambda: self._with_editor(lambda e: e.set_zoom(100)), "Ctrl+0", ()),
            (
                "next_change",
                "Show next change from elsewhere",
                lambda: self._with_editor(lambda e: self._discard(e.next_mark(1))),
                "Alt+F5",
                ("marks",),
            ),
            (
                "clear_marks",
                "Clear changed-elsewhere marks",
                lambda: self._with_editor(lambda e: e.clear_change_marks()),
                "",
                ("marks",),
            ),
            ("rename_note", "Rename note", self.rename_current, "F2", ("title",)),
            ("copy_path", "Copy path of this note", self.copy_current_path, "Ctrl+Shift+C", ()),
            (
                "skipped_notes",
                "Show notes search skips",
                self.show_skipped,
                "",
                ("search", "large", "online", "not searched"),
            ),
            *self.tools.commands(),
        ]
        for command_id, title, run, shortcut, keywords in items:
            self.registry.add(Command(command_id, title, run, shortcut, "Commands", keywords))
            if shortcut:
                action = QAction(title, self)
                action.setShortcut(QKeySequence(shortcut))
                action.setShortcutContext(Qt.ShortcutContext.WindowShortcut)
                action.triggered.connect(run)
                self.addAction(action)
        self.registry.add_provider(self._note_commands)
        self.registry.add_provider(self._editing_commands)
        tree = self.sidebar.tree
        for key, handler in (
            ("Del", lambda: self._sidebar_do(self.library.delete)),
            ("Ctrl+N", lambda: self._discard(self.library.new_note(self.sidebar.current_folder() or ""))),
        ):
            QShortcut(QKeySequence(key), tree, handler, Qt.ShortcutContext.WidgetShortcut)

    @staticmethod
    def _discard(_value: object) -> None:
        return

    def _sidebar_do(self, handler: Callable[[str], object]) -> None:
        path = self.sidebar.current_path()
        if path:
            handler(path)

    def _note_commands(self) -> list[Command]:
        seen: set[str] = set()
        commands: list[Command] = []
        for path in [*self.workbench.paths(), *self._local.recent_notes()]:
            key = path.casefold()
            if key in seen:
                continue
            seen.add(key)
            commands.append(
                Command(
                    f"goto:{path}",
                    f"Go to note: {note_title(path)}",
                    partial(self._go_to, path),
                    "",
                    "Notes",
                    (ntpath.dirname(path),),
                )
            )
        return commands

    def _authoring_host(self, session: NoteSession) -> AuthoringHost:
        def choose_image() -> str | None:
            return self.prompter.choose_image("Insert image", ntpath.dirname(session.path))

        return AuthoringHost(
            note_path=lambda: session.path,
            assets=self._assets,
            choose_image=choose_image,
            ask_table_size=self.prompter.ask_table_size,
            set_clipboard=system_clipboard_text,
            clipboard_mime=system_clipboard_mime,
            toast=self.toast_message,
        )

    def _editing_commands(self) -> list[Command]:
        page = self.workbench.active_page()
        if page is None:
            return []
        authoring = page.authoring
        return [
            Command(
                f"edit:{command.id}",
                command.label,
                partial(self._run_editing, command.id),
                command.shortcut,
                "Editing",
                (command.category.casefold(), *command.keywords),
            )
            for command in authoring.commands.values()
            if command.when is None or command.when(authoring)
        ]

    def _run_editing(self, command_id: str) -> None:
        page = self.workbench.active_page()
        if page is not None:
            page.editor.setFocus(Qt.FocusReason.OtherFocusReason)
            page.authoring.execute(command_id)

    def _go_to(self, path: str) -> None:
        self.open_path(path)

    def _with_editor(self, action: Callable[..., object]) -> None:
        page = self.workbench.active_page()
        if page is not None:
            action(page.editor)

    def open_path(self, path: str, activate: bool = True) -> NoteSession:
        return self.workbench.open_path(path, activate)

    def close_tab(self, index: int) -> bool:
        return self.workbench.close_tab(index)

    def restore_workspace(self) -> None:
        workspace = self._store.load()
        if workspace.window.geometry:
            self.restoreGeometry(QByteArray.fromBase64(workspace.window.geometry.encode("ascii")))
        self.workbench.restore(workspace)

    def _window_state(self) -> WindowState:
        geometry = bytes(self.saveGeometry().toBase64().data()).decode("ascii")
        return WindowState(geometry, "", self.isMaximized())

    def open_dialog(self) -> None:
        path = self.prompter.choose_file("Open note", self.settings.last_used_directory)
        if path:
            self.open_path(path)

    def save_current(self) -> None:
        page = self.workbench.active_page()
        if page is not None:
            page.session.flush(FlushReason.USER)

    def toggle_help(self) -> None:
        self.tool_panel.hide()
        self.help_panel.toggle()

    def _open_note_paths(self) -> list[str]:
        return [s.path for s in self._manager.sessions()]

    def _choose_note_file(self) -> str | None:
        return self.prompter.choose_file("Open note in the second pane", self.settings.last_used_directory)

    def open_palette(self, text: str = "") -> None:
        self.hint.hide()
        self.search_palette.open(text)

    def focus_commands(self) -> None:
        self.open_palette(">")

    def show_skipped(self) -> None:
        self.open_palette("")
        self.search_palette.show_skipped()

    def show_home(self) -> None:
        self.workbench.show_home()

    def _home_changed(self, shown: bool) -> None:
        self.sidebar.set_home(shown and self.workbench.home_visible())
        if shown:
            self.refresh_home()

    def refresh_home(self) -> None:
        if self.workbench.home_visible():
            self.index.overview(self._local.recent_notes(), int((time.time() - WEEK_S) * 1e9), self._overview)

    def _overview(self, overview: Overview) -> None:
        self.workbench.start.set_overview(overview)

    def toggle_sidebar(self) -> None:
        self.sidebar.setVisible(not self.sidebar.isVisible())

    def toggle_maximized(self) -> None:
        if self.isMaximized():
            self.showNormal()
        else:
            self.showMaximized()

    def rename_current(self) -> None:
        if self.sidebar.tree.hasFocus():
            self._sidebar_do(self.library.rename)
            return
        page = self.workbench.active_page()
        if page is not None:
            page.title.setFocus(Qt.FocusReason.ShortcutFocusReason)
            page.title.selectAll()

    def copy_current_path(self) -> None:
        if self.sidebar.tree.hasFocus():
            self._sidebar_do(self.library.copy_path)
            return
        page = self.workbench.active_page()
        if page is not None:
            self.library.copy_path(page.session.path)

    def _copy_tab_path(self, index: int) -> None:
        paths = self.workbench.paths()
        if 0 <= index < len(paths):
            self.library.copy_path(paths[index])

    def _history_for_tab(self, index: int) -> None:
        self.workbench.activate(index)
        self.open_history()

    def _reveal_folder(self, path: str) -> None:
        self.sidebar.setVisible(True)
        self.sidebar.select(path)
        item = self.sidebar.model.item_for(path)
        if item is not None:
            self.sidebar.tree.expand(item.index())
            self.sidebar.tree.scrollTo(item.index())

    def _show_current(self, _page: object) -> None:
        page = self.workbench.active_page()
        session = page.session if page is not None else None
        self.status.show_note(page.editor if page is not None else None, session)
        self.bar.set_note(session.path if session is not None else None, self.sidebar.roots())
        self.bar.set_navigation(
            self.workbench.navigation.can_go_back(), self.workbench.navigation.can_go_forward()
        )
        name = note_title(session.path) if session is not None else ""
        title = f"{name}  \N{MIDDLE DOT}  {PRODUCT_NAME}" if name else PRODUCT_NAME
        self.setWindowTitle(title)
        self.sidebar.set_home(self.workbench.home_visible())
        if session is not None:
            self.sidebar.select(session.path)

    def _on_session_closed(self, _path: str) -> None:
        self._sessions_changed()

    def _sessions_changed(self) -> None:
        self.status.refresh_session()
        sessions = self._manager.sessions()
        tones = {
            pathid.identity(s.path): ("" if session_tone(s) == "ok" else session_tone(s)) for s in sessions
        }
        self.sidebar.set_open_notes(tones)
        self.sidebar.summary.set_summary(*summarize(sessions))
        self.workbench.strip.update()

    def toast_message(self, text: str) -> None:
        self.toast.show_message(text)

    def _confirm(self, title: str, text: str, action: str, danger: bool) -> bool:
        return self.prompter.confirm(title, text, action, danger)

    def _keep(self, dialog: QWidget) -> None:
        self._dialogs = [d for d in self._dialogs if d.isVisible()]
        self._dialogs.append(dialog)

    def open_diff(self, title: str, model: DiffModel | None, note: str) -> DiffDialog:
        dialog = DiffDialog(title, model, self.theme, note, self)
        self._keep(dialog)
        dialog.show()
        return dialog

    def open_conflict(self, session: NoteSession, info: ConflictInfo) -> ConflictResolver:
        resolver = ConflictResolver(session, info, self.theme, self)
        self._keep(resolver)
        resolver.show()
        return resolver

    def open_history(self) -> HistoryDialog | None:
        page = self.workbench.active_page()
        if page is None:
            return None
        session = page.session
        dialog = HistoryDialog(
            session, self.services.history, self.services.evidence, self.theme, self.prompter, parent=self
        )
        dialog.restored.connect(lambda when: self.toast_message(f"Restored the version from {when}"))
        self._keep(dialog)
        dialog.show()
        return dialog

    def open_settings(self) -> SettingsDialog:
        dialog = SettingsDialog(self.settings, self)
        dialog.changed.connect(self.apply_settings)
        self._keep(dialog)
        dialog.show()
        return dialog

    def set_theme_mode(self, mode: str) -> None:
        self.apply_settings(replace(self.settings, theme=mode))

    def apply_settings(self, settings: Settings) -> None:
        previous = self.settings
        self.settings = settings
        self._persist_settings()
        if self._themes is not None:
            self._themes.set_mode(settings.theme)
            self._themes.set_accent(settings.accent)
            self._themes.set_reduced_motion(settings.reduced_motion)
        self.workbench.apply_settings(settings)
        if settings.custom_frame != previous.custom_frame:
            self.toast_message("The window frame changes the next time Paperless Notes starts.")
        if not settings.show_hints:
            self.hint.hide()

    def _persist_settings(self) -> None:
        store = self.services.settings_store
        if store is None:
            return
        try:
            store.save(self.settings)
        except OSError as exc:
            logger.warning("Saving settings failed: %s", exc)
            self.toast_message("Settings could not be saved; they apply until you close the app.")

    def _save_roots(self, roots: list[str]) -> None:
        self.settings = replace(self.settings, library_roots=tuple(roots))
        self._persist_settings()
        self.tools.roots_changed(roots)
        self.workbench.start.set_roots(roots)
        self._show_current(None)
        self.refresh_home()

    def apply_theme(self, current: Theme) -> None:
        self.theme = current
        for widget in (
            self.bar,
            self.sidebar,
            self.toast,
            self.hint,
            self.help_panel,
            self.tool_panel,
            self.workbench,
            self.search_palette,
            self.status,
        ):
            widget.apply_theme(current)
        self._rounder.theme = current
        if not self._native_frame and self.isVisible():
            request_native_decoration(self, self.isMaximized(), current)

    def start_hints(self) -> None:
        self._hints_started = True
        self._next_hint()

    def _next_hint(self) -> None:
        pending = pending_hints(self.settings.show_hints, self.settings.dismissed_hints)
        if not pending:
            self.hint.hide()
            return
        targets = {
            "sidebar": self.sidebar.new_note_button,
            "search": self.bar.search,
            "sync": self.bar.history_button,
        }
        target = targets[pending[0].id]
        if not target.isVisible():
            self.hint.hide()
            return
        self.hint.show_hint(pending[0], target)

    def _dismiss_hint(self, hint_id: str) -> None:
        if hint_id not in self.settings.dismissed_hints:
            self.settings = replace(self.settings, dismissed_hints=(*self.settings.dismissed_hints, hint_id))
            self._persist_settings()
        self._next_hint()

    def _hide_hints(self) -> None:
        self.settings = replace(self.settings, show_hints=False)
        self._persist_settings()
        self.hint.hide()

    def _on_request(self, request: OpenRequest) -> None:
        launch = note_paths(list(request.paths))
        for path in launch.accepted:
            self.open_path(path)
        if launch.refused:
            self.toast_message(refused_message(launch.refused))
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        if not self._native_frame:
            request_native_decoration(self, self.isMaximized(), self.theme)
            allow_snap_layouts(self)
        if not self._hints_started:
            QTimer.singleShot(0, self, self.start_hints)

    def nativeEvent(  # noqa: N802 - Qt override
        self, event_type: QByteArray | bytes | bytearray | memoryview, message: int
    ) -> object:
        kind = event_type.data() if isinstance(event_type, QByteArray) else bytes(event_type)
        if self._maximize_hit is not None and kind == b"windows_generic_MSG":
            result = self._maximize_hit.handle(int(message))
            if result is not None:
                return True, result
        return super().nativeEvent(event_type, message)

    def changeEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        kind = event.type()
        if kind == QEvent.Type.ActivationChange:
            active = self.isActiveWindow()
            self.bar.set_window_active(active)
            if active:
                self._monitor.set_activity(Activity.FOCUSED)
                self._monitor.check_all()
                self.tools.window_activated()
            else:
                self._monitor.set_activity(Activity.UNFOCUSED)
                self._manager.flush_all(FlushReason.DEACTIVATE)
        elif kind == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self._monitor.set_activity(Activity.MINIMIZED)
            self.bar.set_maximized(self.isMaximized())
            if not self._native_frame and self.isVisible():
                request_native_decoration(self, self.isMaximized(), self.theme)
                allow_snap_layouts(self)
        super().changeEvent(event)

    def shutdown(self) -> list[str]:
        """Save everything synchronously (window close or Windows session end)."""
        self.search_palette.close_palette(restore_focus=False)
        try:
            self._store.save_now(self.workbench.current_workspace())
        except OSError as exc:
            logger.warning("Saving the workspace failed: %s", exc)
        unsaved = self._manager.shutdown()
        self.tools.dispose()
        self.index.close()
        self.workbench.split.dispose()
        return unsaved

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802 - Qt override
        unsaved = self.shutdown()
        if unsaved:
            logger.warning("%d note(s) kept in the draft journal at exit", len(unsaved))
        for dialog in self._dialogs:
            dialog.close()
        event.accept()


def install_style(app: QApplication) -> None:
    """Fusion draws every control from the palette and stylesheet, so both themes look the same everywhere;
    the shell style adds the painted tree branches and tabs."""
    app.setStyle(ShellStyle())
