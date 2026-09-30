"""Two-pane split view (ADR-0009): the tabbed pane plus one secondary pane showing one note.

Both panes build their views through the same ``PageFactory``, so two views of one path share one
session, adapter and document, with one saver, monitor, draft stream, ledger and diagnosis; each view
keeps its own caret, selection, scroll and find state. The last-focused pane is the active pane for
commands, the status bar, outline jumps and search results. Closing the split returns to one pane and
closes the note only if no other view still has it open.
"""

from __future__ import annotations

import ntpath
from collections.abc import Callable
from dataclasses import dataclass

from PySide6.QtCore import QMetaObject, QObject, Qt, Signal
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QLabel,
    QMenu,
    QSizePolicy,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core import pathid
from paperless_notes.core.files import is_note_file
from paperless_notes.core.session import FlushReason
from paperless_notes.ui.shell.flow import FlowLayout
from paperless_notes.ui.shell.page import NotePage
from paperless_notes.ui.shell.page_factory import PageFactory

MAX_SPLIT_PATH = 32_767


@dataclass(frozen=True, slots=True)
class SplitState:
    open: bool = False
    path: str = ""


def safe_split_path(path: str) -> bool:
    """A saved secondary path is reopened only when it is an ordinary absolute path to a note file."""
    if not path or len(path) > MAX_SPLIT_PATH or "\x00" in path:
        return False
    if path.startswith(("\\\\", "//")) or not ntpath.isabs(path) or ntpath.splitdrive(path)[0] == "":
        return False
    name = ntpath.basename(path)
    return is_note_file(name) and not name.startswith("~$")


class SecondaryPane(QFrame):
    """The second pane: which note it shows, a way to choose another, Close split, and the note."""

    def __init__(self) -> None:
        super().__init__()
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setObjectName("PaneFrame")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAccessibleName("Second pane")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        header = QWidget()
        header.setObjectName("PaneHeader")
        header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        head = QVBoxLayout(header)
        head.setContentsMargins(8, 4, 4, 4)
        head.setSpacing(4)
        self.title = QLabel("No note")
        self.title.setProperty("role", "secondary")
        self.title.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.title.setWordWrap(True)
        self.title.setAccessibleName("Note in the second pane")
        self.choose_button = QToolButton()
        self.choose_button.setText("Change")
        self.choose_button.setProperty("kind", "labelled")
        self.choose_button.setAccessibleName("Change note in the second pane")
        self.choose_button.setToolTip("Show another open or recent note in this pane")
        self.choose_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.choose_button.setMenu(QMenu(self.choose_button))
        self.close_button = QToolButton()
        self.close_button.setText("Close")
        self.close_button.setProperty("kind", "labelled")
        self.close_button.setAccessibleName("Close split view")
        self.close_button.setToolTip("Close the second pane; the note stays open in its tab")
        head.addWidget(self.title)
        actions_host = QWidget()
        actions = FlowLayout(actions_host, spacing=4)
        actions.addWidget(self.choose_button)
        actions.addWidget(self.close_button)
        head.addWidget(actions_host)
        layout.addWidget(header)
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.empty = QLabel("Choose a note for this pane with Change note.")
        self.empty.setProperty("role", "secondary")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        self.body.addWidget(self.empty, 1)
        layout.addLayout(self.body, 1)


class SplitView(QObject):
    """Opens, fills, switches and closes the secondary pane and tracks which pane is active."""

    active_changed = Signal()
    changed = Signal()

    def __init__(
        self,
        splitter: QSplitter,
        primary: QFrame,
        factory: PageFactory,
        notes: Callable[[], list[str]],
        confirm: Callable[[str, str, str, bool], bool],
        wire: Callable[[NotePage], None] = lambda _page: None,
    ) -> None:
        super().__init__()
        self._splitter = splitter
        self._primary = primary
        self._factory = factory
        self._notes = notes
        self._confirm = confirm
        self._wire = wire
        self.choose_file: Callable[[], str | None] = lambda: None
        self.pane: SecondaryPane | None = None
        self.page: NotePage | None = None
        self._secondary_active = False
        self._remember = ""
        self._rename_connection: QMetaObject.Connection | None = None
        self.focus_primary: Callable[[], None] = lambda: None
        app = QApplication.instance()
        self._focus_connection = (
            app.focusChanged.connect(self._on_focus) if isinstance(app, QApplication) else None
        )

    def is_open(self) -> bool:
        return self.pane is not None

    def path(self) -> str | None:
        return self.page.session.path if self.page is not None else None

    def secondary_active(self) -> bool:
        return self._secondary_active and self.pane is not None

    def state(self) -> SplitState:
        return SplitState(self.is_open(), self.path() or "")

    def open(self, path: str | None = None) -> None:
        """Show the second pane (empty, or with ``path``) and make it active."""
        if self.pane is None:
            self.pane = SecondaryPane()
            self.pane.close_button.clicked.connect(self.close)
            menu = self.pane.choose_button.menu()
            if menu is not None:
                menu.aboutToShow.connect(self._fill_menu)
            self._splitter.addWidget(self.pane)
            self._splitter.setStretchFactor(0, 1)
            self._splitter.setStretchFactor(1, 1)
            total = max(2, sum(self._splitter.sizes()))
            self._splitter.setSizes([total // 2, total - total // 2])
        if path:
            self.show_note(path)
        self._set_active(True)
        self._mark()
        self.changed.emit()

    def show_note(self, path: str) -> NotePage | None:
        """Show ``path`` in the second pane (opening the pane if needed)."""
        if self.pane is None:
            self.open(path)
            return self.page
        if self.page is not None and pathid.same_path(self.page.session.path, path):
            return self.page
        if not self._drop_page():
            return self.page
        page = self._factory.open(path)
        self._wire(page)
        self.page = page
        self.pane.empty.hide()
        self.pane.body.addWidget(page, 1)
        self.pane.title.setText(ntpath.basename(page.session.path))
        self.pane.title.setToolTip(page.session.path)
        self._rename_connection = page.session.path_changed.connect(self._on_renamed)
        self._set_active(True)
        self.changed.emit()
        return page

    def close(self) -> bool:
        """Return to one pane. False when the user keeps the pane because its note could not be saved."""
        if self.pane is None:
            return True
        if not self._drop_page():
            return False
        pane, self.pane = self.pane, None
        pane.hide()
        pane.setParent(None)
        pane.deleteLater()
        self._set_active(False)
        self._mark()
        self.changed.emit()
        return True

    def focus_other(self) -> None:
        """Move the keyboard to the other pane's text (F6)."""
        if self.pane is None:
            return
        if self._secondary_active:
            self._set_active(False)
            self.focus_primary()
        elif self.page is not None:
            self._set_active(True)
            self.page.editor.setFocus(Qt.FocusReason.ShortcutFocusReason)
        else:
            self._set_active(True)
            self.pane.choose_button.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def activate(self, page: NotePage) -> None:
        """Make the pane holding ``page`` the active one (a control in it was used)."""
        self._set_active(page is self.page and self.page is not None)

    def release_for(self, path: str) -> bool:
        """Let go of the note before a rename or move of ``path`` (the note or a folder holding it).
        False only when the note could not be saved first; then nothing is released."""
        current = self.path()
        if current is None or not (pathid.same_path(current, path) or pathid.is_within(current, path)):
            return True
        session = self.page.session if self.page is not None else None
        if session is not None and session.dirty and not session.flush_blocking():
            return False
        self._remember = current
        return self._drop_page(ask=False)

    def rebind(self, old: str, new: str) -> None:
        """Show the renamed or moved note again after ``release_for``."""
        remembered, self._remember = self._remember, ""
        if not remembered or self.pane is None:
            return
        if pathid.same_path(remembered, old):
            self.show_note(new)
        elif pathid.is_within(remembered, old):
            self.show_note(new + remembered[len(old.rstrip("\\")) :])
        else:
            self.show_note(remembered)

    def forget(self, path: str) -> None:
        """The note (or a folder holding it) was deleted: show the empty pane."""
        current = self.path()
        if current is not None and (pathid.same_path(current, path) or pathid.is_within(current, path)):
            self._drop_page(ask=False)

    def dispose(self) -> None:
        if self._focus_connection is not None:
            QObject.disconnect(self._focus_connection)
            self._focus_connection = None

    def _drop_page(self, ask: bool = True) -> bool:
        page = self.page
        if page is None:
            return True
        session = page.session
        last = self._factory.manager.view_count(session) <= 1
        if (
            ask
            and last
            and session.dirty
            and not session.flush_blocking()
            and not self._confirm(
                "Not saved",
                f"{ntpath.basename(session.path)} could not be saved. Close it anyway? The text is kept in "
                "the recovery journal and offered again when you reopen the note.",
                "Close anyway",
                True,
            )
        ):
            return False
        if not last:
            session.flush(FlushReason.TAB_SWITCH)
        if self._rename_connection is not None:
            QObject.disconnect(self._rename_connection)
            self._rename_connection = None
        self.page = None
        if self.pane is not None:
            self.pane.body.removeWidget(page)
            self.pane.empty.show()
            self.pane.title.setText("No note")
            self.pane.title.setToolTip("")
        page.hide()
        self._factory.close(page)
        page.deleteLater()
        self.changed.emit()
        return True

    def _on_renamed(self, path: str) -> None:
        if self.pane is not None:
            self.pane.title.setText(ntpath.basename(path))
            self.pane.title.setToolTip(path)
        self.changed.emit()

    def _fill_menu(self) -> None:
        if self.pane is None:
            return
        menu = self.pane.choose_button.menu()
        if menu is None:
            return
        menu.clear()
        seen: set[str] = set()
        for path in self._notes():
            key = pathid.identity(path)
            if key in seen:
                continue
            seen.add(key)
            action = menu.addAction(ntpath.basename(path))
            action.setToolTip(path)
            action.triggered.connect(lambda _c=False, p=path: self.show_note(p))
        if seen:
            menu.addSeparator()
        action = menu.addAction("Open a file...")
        action.triggered.connect(self._choose_file)

    def _choose_file(self) -> None:
        path = self.choose_file()
        if path:
            self.show_note(path)

    def _on_focus(self, _old: QWidget | None, new: QWidget | None) -> None:
        if new is None or self.pane is None:
            return
        if self.pane.isAncestorOf(new):
            self._set_active(True)
        elif self._primary.isAncestorOf(new):
            self._set_active(False)

    def _set_active(self, secondary: bool) -> None:
        secondary = secondary and self.pane is not None
        if secondary == self._secondary_active:
            return
        self._secondary_active = secondary
        self._mark()
        self.active_changed.emit()

    def _mark(self) -> None:
        """The active pane carries the focus colour along its top edge while the split is open."""
        split = self.pane is not None
        for frame, active in (
            (self._primary, not self._secondary_active),
            (self.pane, self._secondary_active),
        ):
            if frame is None:
                continue
            frame.setProperty("active", "true" if split and active else "false")
            style = frame.style()
            style.unpolish(frame)
            style.polish(frame)
