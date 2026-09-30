"""The sidebar: Home, Search and New note rows, the library (configured folders as a lazy tree with painted
rows, indentation guides, a New note button on hovered folders and dots on open notes) and a sync summary of
the open notes."""

from __future__ import annotations

import logging
import ntpath
import os
from collections.abc import Callable

from PySide6.QtCore import (
    QEvent,
    QModelIndex,
    QPersistentModelIndex,
    QPoint,
    QPointF,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QFontMetrics,
    QHelpEvent,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QLabel,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QToolTip,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core import pathid
from paperless_notes.core.files import is_note_file
from paperless_notes.ui.shell.labels import note_title
from paperless_notes.ui.shell.widgets import IDLE, RowButton, SectionLabel, meta_font, paint_dot
from paperless_notes.ui.theme.icons import draw_glyph, glyph_icon
from paperless_notes.ui.theme.tokens import Theme

logger = logging.getLogger(__name__)

PATH_ROLE = Qt.ItemDataRole.UserRole + 1
KIND_ROLE = Qt.ItemDataRole.UserRole + 2
LOADED_ROLE = Qt.ItemDataRole.UserRole + 3
MAX_ENTRIES = 5_000
ROOT, FOLDER, NOTE, PLACEHOLDER = "root", "folder", "note", "placeholder"
PLUS_SIZE = 20


def list_folder(folder: str) -> list[tuple[str, bool]]:
    """Folders first, then notes, each sorted without case; hidden folders and temp files are skipped."""
    entries: list[tuple[str, bool]] = []
    try:
        with os.scandir(pathid.to_os_path(folder)) as scan:
            for entry in scan:
                if len(entries) >= MAX_ENTRIES:
                    break
                try:
                    is_dir = entry.is_dir()
                except OSError:
                    continue
                if is_dir and not entry.name.startswith("."):
                    entries.append((entry.name, True))
                elif not is_dir and is_note_file(entry.name):
                    entries.append((entry.name, False))
    except OSError as exc:
        logger.info("Cannot list a library folder: %s", exc)
    return sorted(entries, key=lambda e: (not e[1], e[0].casefold()))


class LibraryModel(QStandardItemModel):
    def __init__(self, lister: Callable[[str], list[tuple[str, bool]]] = list_folder) -> None:
        super().__init__()
        self._lister = lister
        self._icons: dict[str, QIcon] = {}

    def set_icons(self, folder: QIcon, note: QIcon) -> None:
        self._icons = {FOLDER: folder, ROOT: folder, NOTE: note}

    def _item(self, path: str, kind: str, label: str | None = None) -> QStandardItem:
        item = QStandardItem(label or ntpath.basename(path.rstrip("\\")) or path)
        item.setEditable(False)
        item.setData(path, PATH_ROLE)
        item.setData(kind, KIND_ROLE)
        item.setToolTip(path)
        if kind in (ROOT, FOLDER):
            item.setData(False, LOADED_ROLE)
            placeholder = QStandardItem("Loading")
            placeholder.setData(PLACEHOLDER, KIND_ROLE)
            placeholder.setEditable(False)
            item.appendRow(placeholder)
        return item

    def set_roots(self, roots: list[str]) -> None:
        self.clear()
        for root in roots:
            self.appendRow(self._item(root, ROOT))

    def populate(self, item: QStandardItem) -> None:
        if item.data(KIND_ROLE) not in (ROOT, FOLDER):
            return
        item.removeRows(0, item.rowCount())
        folder = str(item.data(PATH_ROLE))
        for name, is_dir in self._lister(folder):
            item.appendRow(self._item(ntpath.join(folder, name), FOLDER if is_dir else NOTE, name))
        item.setData(True, LOADED_ROLE)

    def item_for(self, path: str) -> QStandardItem | None:
        key = pathid.identity(path)
        stack = [self.item(r) for r in range(self.rowCount())]
        while stack:
            item = stack.pop()
            if item is None:
                continue
            if item.data(PATH_ROLE) and pathid.identity(str(item.data(PATH_ROLE))) == key:
                return item
            stack.extend(item.child(r) for r in range(item.rowCount()))
        return None


class LibraryTree(QTreeView):
    """Full-width rounded row highlights; the rows themselves are painted by ``LibraryDelegate``."""

    new_note_in = Signal(str)

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.setObjectName("LibraryTree")
        self.theme = theme
        self.hover = QPersistentModelIndex()
        self.setMouseTracking(True)
        self.setIndentation(16)
        self.setUniformRowHeights(True)
        self.setHeaderHidden(True)
        self.setAnimated(not theme.reduced_motion)
        self.setExpandsOnDoubleClick(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def drawRow(  # noqa: N802 - Qt override
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        p = self.theme.palette
        row = QRectF(option.rect)  # type: ignore[attr-defined]
        rect = QRectF(4, row.top() + 1, self.viewport().width() - 8, row.height() - 2)
        selected = self.selectionModel() is not None and self.selectionModel().isSelected(index)
        hovered = self.hover.isValid() and QPersistentModelIndex(index) == self.hover
        if selected or hovered:
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.selected if selected else p.hover))
            painter.drawRoundedRect(rect, self.theme.radius.md, self.theme.radius.md)
            painter.restore()
        super().drawRow(painter, option, index)
        if (
            selected
            and self.hasFocus()
            and self.window().testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange)
        ):
            painter.save()
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(QPen(QColor(p.focus), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                rect.adjusted(0.75, 0.75, -0.75, -0.75), self.theme.radius.md, self.theme.radius.md
            )
            painter.restore()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        index = self.indexAt(event.position().toPoint())
        hover = QPersistentModelIndex(index) if index.isValid() else QPersistentModelIndex()
        if hover != self.hover:
            self.hover = hover
            self.viewport().update()
        super().mouseMoveEvent(event)

    def leaveEvent(self, event: QEvent) -> None:  # noqa: N802 - Qt override
        self.hover = QPersistentModelIndex()
        self.viewport().update()
        super().leaveEvent(event)


class LibraryDelegate(QStyledItemDelegate):
    """Glyph and name (notes without their .md extension), a storage tag on library folders, a dot on notes
    that are open, and a New note button on the hovered folder."""

    def __init__(self, tree: LibraryTree, sidebar: Sidebar) -> None:
        super().__init__(tree)
        self._tree = tree
        self._sidebar = sidebar

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:  # noqa: N802
        return QSize(120, self._tree.theme.metrics.row)

    def _plus_rect(self, option_rect: QRect) -> QRect:
        right = self._tree.viewport().width() - 8
        return QRect(
            right - PLUS_SIZE - 2, option_rect.center().y() - PLUS_SIZE // 2 + 1, PLUS_SIZE, PLUS_SIZE
        )

    def _folder_hovered(self, index: QModelIndex | QPersistentModelIndex) -> bool:
        hover = self._tree.hover
        return (
            hover.isValid()
            and QPersistentModelIndex(index) == hover
            and index.data(KIND_ROLE) in (ROOT, FOLDER)
        )

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        theme = self._tree.theme
        p = theme.palette
        rect = QRect(option.rect)  # type: ignore[attr-defined]
        kind = index.data(KIND_ROLE)
        path = str(index.data(PATH_ROLE) or "")
        state = option.state  # type: ignore[attr-defined]
        selected = bool(state & QStyle.StateFlag.State_Selected)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        mid = rect.center().y() + 0.5
        if kind == PLACEHOLDER:
            painter.setPen(QColor(p.text_muted))
            painter.drawText(rect.adjusted(24, 0, 0, 0), int(Qt.AlignmentFlag.AlignVCenter), "Loading")
            painter.restore()
            return
        glyph = "library" if kind == ROOT else "folder" if kind == FOLDER else "note"
        emphasis = selected or self._folder_hovered(index)
        draw_glyph(
            painter,
            glyph,
            QRectF(rect.left() + 2, mid - 8, 16, 16),
            QColor(p.text if emphasis else p.text_muted if kind == NOTE else p.text_secondary),
        )
        right = self._tree.viewport().width() - 12
        if self._folder_hovered(index):
            plus = self._plus_rect(rect)
            draw_glyph(painter, "plus", QRectF(plus).adjusted(4, 4, -4, -4), QColor(p.text_secondary), 1.6)
            right = plus.left() - 6
        elif kind == ROOT:
            tag = "ONEDRIVE" if self._sidebar.synced(path) else "THIS PC"
            font = meta_font(theme)
            painter.setFont(font)
            painter.setPen(QColor(p.text_muted))
            width = QFontMetrics(font).horizontalAdvance(tag)
            painter.drawText(
                QRectF(right - width, rect.top(), width, rect.height()),
                int(Qt.AlignmentFlag.AlignVCenter),
                tag,
            )
            right -= width + 10
        elif kind == NOTE:
            tone = self._sidebar.open_tone(path)
            if tone is not None:
                paint_dot(painter, QPointF(right - 3, mid), tone, p, 3.0)
                right -= 14
        name = str(index.data(Qt.ItemDataRole.DisplayRole) or "")
        label = note_title(name) if kind == NOTE else name
        font = self._tree.font()
        if kind == ROOT:
            font.setWeight(font.Weight.DemiBold)
        painter.setFont(font)
        painter.setPen(QColor(p.text if kind != NOTE or selected else p.text_secondary))
        metrics = QFontMetrics(font)
        left = rect.left() + 26
        painter.drawText(
            QRectF(left, rect.top(), max(0, right - left), rect.height()),
            int(Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(label, Qt.TextElideMode.ElideRight, max(0, right - left)),
        )
        painter.restore()

    def editorEvent(  # noqa: N802 - Qt override
        self,
        event: QEvent,
        model: object,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        if (
            event.type() == QEvent.Type.MouseButtonRelease
            and isinstance(event, QMouseEvent)
            and event.button() == Qt.MouseButton.LeftButton
            and index.data(KIND_ROLE) in (ROOT, FOLDER)
            and self._plus_rect(QRect(option.rect)).contains(event.position().toPoint())  # type: ignore[attr-defined]
        ):
            self._tree.new_note_in.emit(str(index.data(PATH_ROLE)))
            return True
        if (
            event.type() in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick)
            and isinstance(event, QMouseEvent)
            and index.data(KIND_ROLE) in (ROOT, FOLDER)
            and self._plus_rect(QRect(option.rect)).contains(event.position().toPoint())  # type: ignore[attr-defined]
        ):
            return True
        return super().editorEvent(event, model, option, index)  # type: ignore[arg-type]

    def helpEvent(  # noqa: N802 - Qt override
        self,
        event: QHelpEvent,
        view: QAbstractItemView,
        option: QStyleOptionViewItem,
        index: QModelIndex | QPersistentModelIndex,
    ) -> bool:
        plus = self._plus_rect(QRect(option.rect))  # type: ignore[attr-defined]
        if index.data(KIND_ROLE) in (ROOT, FOLDER) and plus.contains(event.pos()):
            QToolTip.showText(
                event.globalPos(), f"New note in {index.data(Qt.ItemDataRole.DisplayRole)}", view
            )
            return True
        return super().helpEvent(event, view, option, index)


class SyncSummary(QWidget):
    """The worst state among open notes, in words, with a dot."""

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self._theme = theme
        self.tone = IDLE
        self.text = "No notes open"
        self.annotation = ""
        self.setFixedHeight(36)
        self.setAccessibleName(self.text)

    def set_summary(self, tone: str, text: str, annotation: str = "", detail: str = "") -> None:
        self.tone, self.text, self.annotation = tone, text, annotation
        self.setAccessibleName(text)
        self.setToolTip(detail or text)
        self.update()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        p = self._theme.palette
        painter = QPainter(self)
        painter.setPen(QPen(QColor(p.border), 1))
        painter.drawLine(QPoint(0, 0), QPoint(self.width(), 0))
        mid = self.height() / 2 + 0.5
        paint_dot(painter, QPointF(20, mid), self.tone, p, 3.5)
        right = self.width() - 12
        if self.annotation:
            font = meta_font(self._theme)
            painter.setFont(font)
            painter.setPen(QColor(p.text_muted))
            width = QFontMetrics(font).horizontalAdvance(self.annotation)
            painter.drawText(
                QRectF(right - width, 0, width, self.height()),
                int(Qt.AlignmentFlag.AlignVCenter),
                self.annotation,
            )
            right -= width + 10
        painter.setFont(self.font())
        painter.setPen(QColor(p.text_secondary))
        painter.drawText(
            QRectF(34, 0, max(0, right - 34), self.height()),
            int(Qt.AlignmentFlag.AlignVCenter),
            self.fontMetrics().elidedText(self.text, Qt.TextElideMode.ElideRight, max(0, right - 34)),
        )


class Sidebar(QWidget):
    open_requested = Signal(str)
    new_note_requested = Signal(str)
    new_folder_requested = Signal(str)
    add_root_requested = Signal()
    remove_root_requested = Signal(str)
    rename_requested = Signal(str)
    move_requested = Signal(str)
    delete_requested = Signal(str)
    copy_path_requested = Signal(str)
    home_requested = Signal()
    search_requested = Signal()

    def __init__(
        self,
        theme: Theme,
        lister: Callable[[str], list[tuple[str, bool]]] = list_folder,
        synced: Callable[[str], bool] | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("Sidebar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(theme.metrics.sidebar_min)
        self._theme = theme
        self._roots: list[str] = []
        self._expanded: set[str] = set()
        self._open: dict[str, str] = {}
        self.synced = synced or (lambda _path: False)
        s = theme.spacing
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, s.md, 0, 0)
        layout.setSpacing(0)
        nav = QVBoxLayout()
        nav.setContentsMargins(s.sm, 0, s.sm, 0)
        nav.setSpacing(1)
        self.home_button = RowButton("Home", "home", theme, annotation="")
        self.home_button.setCheckable(True)
        self.home_button.setToolTip("Recent notes, changes and tags (Alt+Home)")
        self.home_button.clicked.connect(self.home_requested.emit)
        self.search_button = RowButton("Search", "search", theme, "Ctrl+Shift+P")
        self.search_button.setToolTip("Search note names and text; type > for commands (Ctrl+Shift+P)")
        self.search_button.clicked.connect(self.search_requested.emit)
        self.new_note_button = RowButton("New note", "new_note", theme, "Ctrl+N")
        self.new_note_button.setToolTip("New note in the selected folder (Ctrl+N)")
        self.new_note_button.clicked.connect(
            lambda: self.new_note_requested.emit(self.current_folder() or "")
        )
        for row in (self.home_button, self.search_button, self.new_note_button):
            nav.addWidget(row)
        layout.addLayout(nav)
        layout.addSpacing(s.xl)
        self.new_folder_button = QToolButton()
        self.new_folder_button.setAccessibleName("New folder")
        self.new_folder_button.setToolTip("New folder inside the selected folder")
        self.new_folder_button.clicked.connect(
            lambda: self.new_folder_requested.emit(self.current_folder() or "")
        )
        self.add_folder_button = QToolButton()
        self.add_folder_button.setAccessibleName("Add folder")
        self.add_folder_button.setToolTip("Add a folder on this PC to the library")
        self.add_folder_button.clicked.connect(self.add_root_requested.emit)
        self.heading = SectionLabel("Library", theme, (self.new_folder_button, self.add_folder_button))
        self.heading.setContentsMargins(s.lg + s.xxs, 0, s.md, 0)
        layout.addWidget(self.heading)
        layout.addSpacing(s.xs)
        self.model = LibraryModel(lister)
        self.tree = LibraryTree(theme)
        self.tree.setAccessibleName("Library folders and notes")
        self.tree.setModel(self.model)
        self.tree.setItemDelegate(LibraryDelegate(self.tree, self))
        self.tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.tree.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._context_menu)
        self.tree.expanded.connect(self._on_expanded)
        self.tree.collapsed.connect(self._on_collapsed)
        self.tree.activated.connect(self._on_activated)
        self.tree.new_note_in.connect(self.new_note_requested.emit)
        tree_box = QVBoxLayout()
        tree_box.setContentsMargins(s.xs, 0, 0, 0)
        tree_box.addWidget(self.tree)
        layout.addLayout(tree_box, 1)
        self.empty = QLabel(
            "No folders yet. Use Add folder (the button next to Library) to show your notes here."
        )
        self.empty.setWordWrap(True)
        self.empty.setProperty("role", "muted")
        self.empty.setContentsMargins(s.lg + s.xxs, 0, s.lg, s.lg)
        layout.addWidget(self.empty)
        self.summary = SyncSummary(theme)
        layout.addWidget(self.summary)
        self.apply_theme(theme)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        p = theme.palette
        self.tree.theme = theme
        self.tree.viewport().update()
        for row in (self.home_button, self.search_button, self.new_note_button):
            row.apply_theme(theme)
        self.heading.apply_theme(theme)
        self.summary.apply_theme(theme)
        self.new_folder_button.setIcon(glyph_icon("new_folder", p.text_muted, p.text))
        self.add_folder_button.setIcon(glyph_icon("add_folder", p.text_muted, p.text))

    def set_home(self, shown: bool) -> None:
        self.home_button.setChecked(shown)

    def set_open_notes(self, tones: dict[str, str]) -> None:
        """Identity of each open note mapped to its state tone ("" for a saved note)."""
        if tones != self._open:
            self._open = dict(tones)
            self.tree.viewport().update()

    def open_tone(self, path: str) -> str | None:
        tone = self._open.get(pathid.identity(path))
        if tone is None:
            return None
        return tone or IDLE

    def set_roots(self, roots: list[str]) -> None:
        self._roots = list(roots)
        self.refresh()

    def roots(self) -> list[str]:
        return list(self._roots)

    def refresh(self) -> None:
        """Rebuild the tree, keeping expanded folders and the selection where they still exist."""
        selected = self.current_path()
        self.model.set_roots(self._roots)
        self.empty.setVisible(not self._roots)
        for path in sorted(self._expanded, key=len):
            item = self.model.item_for(path)
            if item is not None:
                self.tree.expand(item.index())
        if selected:
            self.select(selected)

    def select(self, path: str) -> bool:
        item = self.model.item_for(path)
        if item is None:
            return False
        self.tree.setCurrentIndex(item.index())
        return True

    def current_path(self) -> str | None:
        index = self.tree.currentIndex()
        value = index.data(PATH_ROLE) if index.isValid() else None
        return str(value) if value else None

    def current_folder(self) -> str | None:
        index = self.tree.currentIndex()
        if not index.isValid():
            return self._roots[0] if self._roots else None
        path = str(index.data(PATH_ROLE))
        return path if index.data(KIND_ROLE) in (ROOT, FOLDER) else ntpath.dirname(path)

    def _on_expanded(self, index: QModelIndex) -> None:
        item = self.model.itemFromIndex(index)
        if item is not None and not item.data(LOADED_ROLE):
            self.model.populate(item)
        path = index.data(PATH_ROLE)
        if path:
            self._expanded.add(str(path))

    def _on_collapsed(self, index: QModelIndex) -> None:
        path = index.data(PATH_ROLE)
        if path:
            self._expanded.discard(str(path))

    def _on_activated(self, index: QModelIndex) -> None:
        if index.data(KIND_ROLE) == NOTE:
            self.open_requested.emit(str(index.data(PATH_ROLE)))

    def menu_for(self, index: QModelIndex) -> QMenu:
        menu = QMenu(self)
        kind = index.data(KIND_ROLE)
        path = str(index.data(PATH_ROLE) or "")

        def add(text: str, handler: Callable[[], None], shortcut: str = "") -> QAction:
            action = menu.addAction(text)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(handler)
            return action

        if kind == NOTE:
            add("Open", lambda: self.open_requested.emit(path), "Return")
            menu.addSeparator()
        if kind in (ROOT, FOLDER):
            add("New note here", lambda: self.new_note_requested.emit(path), "Ctrl+N")
            add("New folder here", lambda: self.new_folder_requested.emit(path))
            menu.addSeparator()
        if kind in (NOTE, FOLDER):
            add("Rename", lambda: self.rename_requested.emit(path), "F2")
            add("Move to folder", lambda: self.move_requested.emit(path))
        add("Copy path", lambda: self.copy_path_requested.emit(path), "Ctrl+Shift+C")
        if kind in (NOTE, FOLDER):
            menu.addSeparator()
            add("Delete to Recycle Bin", lambda: self.delete_requested.emit(path), "Del")
        if kind == ROOT:
            menu.addSeparator()
            add("Remove from library", lambda: self.remove_root_requested.emit(path))
        return menu

    def _context_menu(self, pos: QPoint) -> None:
        index = self.tree.indexAt(pos)
        if not index.isValid() or index.data(KIND_ROLE) == PLACEHOLDER:
            return
        self.tree.setCurrentIndex(index)
        self.menu_for(index).popup(self.tree.viewport().mapToGlobal(pos))
