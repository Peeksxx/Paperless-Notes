"""One tab group: a tab strip over the pages of the group's notes. Up to three groups sit side by side; the
first also holds Home. A group takes tabs dragged out of another group's strip and dropped anywhere on it."""

from __future__ import annotations

import itertools
import ntpath
from dataclasses import dataclass

from PySide6.QtCore import QEvent, QMetaObject, QObject, Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QSizePolicy,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core import pathid
from paperless_notes.core.files import is_note_file
from paperless_notes.core.session import NoteSession
from paperless_notes.ui.shell.page import NotePage
from paperless_notes.ui.shell.tabs import TabOverflow, TabStrip, read_tab_mime
from paperless_notes.ui.theme.icons import glyph_icon
from paperless_notes.ui.theme.tokens import Theme

MAX_NOTE_PATH = 32_767
_KEYS = itertools.count(1)


def safe_note_path(path: str) -> bool:
    """A saved tab of a group after the first is restored only when it is an ordinary absolute path to a
    note file."""
    if not path or len(path) > MAX_NOTE_PATH or "\x00" in path:
        return False
    if path.startswith(("\\\\", "//")) or not ntpath.isabs(path) or ntpath.splitdrive(path)[0] == "":
        return False
    name = ntpath.basename(path)
    return is_note_file(name) and not name.startswith("~$")


@dataclass(eq=False)
class OpenTab:
    path: str
    pinned: bool = False
    cursor: int = 0
    scroll: int = 0
    session: NoteSession | None = None
    page: NotePage | None = None
    connections: list[QMetaObject.Connection] | None = None


class TabGroup(QFrame):
    """The strip row (tabs, new note, all tabs) and a stack of the open pages, plus Home in the first group.
    ``tab_dropped`` reports a tab from the group with key ``source`` dropped here, before ``at`` (-1 for the
    end)."""

    tab_dropped = Signal(str, int, int)

    def __init__(self, theme: Theme, start: QWidget | None = None) -> None:
        super().__init__()
        self.key = str(next(_KEYS))
        self.setObjectName("PaneFrame")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Ignored)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAcceptDrops(True)
        self.tabs: list[OpenTab] = []
        self.home = False
        self._theme = theme
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        strip_row = QHBoxLayout()
        strip_row.setContentsMargins(theme.spacing.xs, 0, theme.spacing.xs, 0)
        strip_row.setSpacing(theme.spacing.xxs)
        self.strip = TabStrip(theme)
        self.strip.drag_key = self.key
        self.overflow = TabOverflow(self.strip, theme)
        self.new_tab_button = QToolButton()
        self.new_tab_button.setObjectName("StripButton")
        self.new_tab_button.setAccessibleName("New note")
        self.new_tab_button.setToolTip("New note (Ctrl+N)")
        strip_row.addWidget(self.strip)
        strip_row.addWidget(self.new_tab_button, 0, Qt.AlignmentFlag.AlignVCenter)
        strip_row.addStretch(1)
        strip_row.addWidget(self.overflow, 0, Qt.AlignmentFlag.AlignVCenter)
        self.strip_host = QWidget()
        self.strip_host.setObjectName("TabStripHost")
        self.strip_host.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.strip_host.setLayout(strip_row)
        self.strip_host.installEventFilter(self)
        layout.addWidget(self.strip_host)
        self.stack = QStackedWidget()
        self.start = start
        if start is not None:
            self.stack.addWidget(start)
        layout.addWidget(self.stack, 1)
        self.apply_theme(theme)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.strip.apply_theme(theme)
        self.overflow.apply_theme(theme)
        self.new_tab_button.setIcon(glyph_icon("plus", theme.palette.text_secondary, theme.palette.text))

    def paths(self) -> list[str]:
        return [t.path for t in self.tabs]

    def index_of(self, path: str) -> int:
        key = pathid.identity(path)
        return next((i for i, t in enumerate(self.tabs) if pathid.identity(t.path) == key), -1)

    def current_page(self) -> NotePage | None:
        index = self.strip.currentIndex()
        return self.tabs[index].page if 0 <= index < len(self.tabs) else None

    def home_visible(self) -> bool:
        return self.start is not None and self.stack.currentWidget() is self.start

    def mark(self, active: bool, shown: bool) -> None:
        """The active group carries the focus colour along its top edge while more than one group shows."""
        self.strip.set_pane_active(active)
        self.setProperty("active", "true" if shown and active else "false")
        style = self.style()
        style.unpolish(self)
        style.polish(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is self.strip_host and event.type() == QEvent.Type.Resize:
            self.fit_tabs()
        return super().eventFilter(watched, event)

    def fit_tabs(self) -> None:
        """Give the tabs the room left beside the new-tab and all-tabs buttons."""
        row = self.strip_host.layout()
        if row is None:
            return
        margins = row.contentsMargins()
        buttons = self.new_tab_button.sizeHint().width() + self.overflow.sizeHint().width()
        spare = self.strip_host.width() - margins.left() - margins.right() - buttons - 3 * row.spacing()
        self.strip.set_available(max(0, spare - self._theme.spacing.md))

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if read_tab_mime(event.mimeData()) is not None:
            event.acceptProposedAction()

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if read_tab_mime(event.mimeData()) is not None:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        carried = read_tab_mime(event.mimeData())
        if carried is None:
            return
        event.acceptProposedAction()
        point = self.strip.mapFrom(self, event.position().toPoint())
        at = self.strip.tabAt(point) if self.strip.rect().contains(point) else -1
        self.tab_dropped.emit(carried[0], carried[1], at)
