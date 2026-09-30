"""The search palette (Ctrl+Shift+P): one field for notes, commands, tags, headings and line numbers.

Plain text searches note names and text. A leading ``>`` lists commands, ``#`` lists tags, ``@`` lists the
headings of the active note, ``:`` goes to a line and ``?`` explains the prefixes. The footer always shows
the prefixes, and each one can be clicked.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

import shiboken6
from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QModelIndex,
    QObject,
    QPersistentModelIndex,
    QPoint,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QHelpEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPaintEvent,
    QPen,
    QStandardItem,
    QStandardItemModel,
)
from PySide6.QtWidgets import (
    QApplication,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLineEdit,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core.search_index import (
    IndexState,
    IndexStatus,
    SearchIndex,
    SearchResult,
    SkippedNote,
    TagCount,
)
from paperless_notes.core.search_query import compile_query
from paperless_notes.mdio.outline import Outline
from paperless_notes.ui.shell.commands import CommandRegistry
from paperless_notes.ui.shell.labels import location_of, note_title
from paperless_notes.ui.shell.widgets import caption_font, meta_font, paint_keycaps, paint_shadow
from paperless_notes.ui.theme.icons import draw_glyph
from paperless_notes.ui.theme.tokens import Theme

NOTES, COMMANDS, TAGS, HEADINGS, LINE, HELP, SKIPPED = (
    "notes",
    "commands",
    "tags",
    "headings",
    "line",
    "help",
    "skipped",
)
PREFIXES: tuple[tuple[str, str, str, str], ...] = (
    (">", COMMANDS, "Commands", "Run any command by name"),
    ("#", TAGS, "Tags", "Every tag, then the notes that carry it"),
    ("@", HEADINGS, "Headings", "Jump to a heading in the note you are in"),
    (":", LINE, "Line", "Go to a line number in the note you are in"),
    ("?", HELP, "Help", "What each prefix does"),
)
ITEM_ROLE = Qt.ItemDataRole.UserRole + 1
SEARCH_DEBOUNCE_MS = 90
SHADOW = 18
MAX_ROWS_HEIGHT = 420


@dataclass(frozen=True)
class PaletteItem:
    title: str
    kind: str = "note"
    glyph: str = "note"
    detail: str = ""
    marks: tuple[tuple[int, int], ...] = ()
    annotation: str = ""
    shortcut: str = ""
    level: int = 0
    header: bool = False
    run: Callable[[], object] | None = field(default=None, compare=False)


@dataclass
class PaletteSources:
    registry: CommandRegistry
    index: SearchIndex | None
    open_notes: Callable[[], list[str]]
    recent_notes: Callable[[], list[str]]
    roots: Callable[[], list[str]]
    open_note: Callable[[str, tuple[str, ...]], object]
    outline: Callable[[], Outline | None]
    reveal_heading: Callable[[int], object]
    line_count: Callable[[], int | None]
    go_to_line: Callable[[int], object]


def parse(text: str) -> tuple[str, str]:
    """The mode a query selects and the query without its prefix."""
    for prefix, mode, _label, _about in PREFIXES:
        if prefix != "#" and text.startswith(prefix):
            return mode, text[len(prefix) :].strip()
    if re.fullmatch(r"#[^\s#]*", text):
        return TAGS, text[1:]
    return NOTES, text.strip()


def _name_match(query: str, path: str) -> bool:
    name = note_title(path).casefold()
    return all(word in name for word in query.casefold().split())


class _Delegate(QStyledItemDelegate):
    def __init__(self, palette: SearchPalette) -> None:
        super().__init__(palette)
        self._palette = palette

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex) -> QSize:  # noqa: N802
        item = index.data(ITEM_ROLE)
        if not isinstance(item, PaletteItem):
            return QSize(100, 34)
        if item.header:
            return QSize(100, 28)
        return QSize(100, 48 if item.detail and item.kind == "note" else 34)

    def paint(
        self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex | QPersistentModelIndex
    ) -> None:
        item = index.data(ITEM_ROLE)
        if not isinstance(item, PaletteItem):
            return
        theme = self._palette.theme
        p = theme.palette
        rect = QRectF(option.rect)  # type: ignore[attr-defined]
        state = option.state  # type: ignore[attr-defined]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if item.header:
            painter.setFont(caption_font(theme))
            painter.setPen(QColor(p.text_muted))
            painter.drawText(rect.adjusted(14, 6, -14, 0), int(Qt.AlignmentFlag.AlignVCenter), item.title)
            painter.restore()
            return
        selected = bool(state & QStyle.StateFlag.State_Selected)
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        if selected or hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.selected if selected else p.hover))
            painter.drawRoundedRect(rect.adjusted(6, 1, -6, -1), theme.radius.md, theme.radius.md)
        if selected:
            painter.setBrush(QColor(p.accent))
            painter.drawRoundedRect(
                QRectF(rect.left() + 6, rect.top() + 9, 2.5, rect.height() - 18), 1.2, 1.2
            )
        enabled = item.run is not None
        indent = 14 * item.level
        two_lines = bool(item.detail) and item.kind == "note"
        title_mid = rect.top() + (17 if two_lines else rect.height() / 2)
        glyph_color = QColor(p.text if selected else p.text_secondary if enabled else p.text_muted)
        draw_glyph(painter, item.glyph, QRectF(rect.left() + 18 + indent, title_mid - 8, 16, 16), glyph_color)
        right = rect.right() - 16
        if item.shortcut:
            right -= paint_keycaps(painter, right, title_mid, item.shortcut, meta_font(theme), p) + 10
        if item.annotation:
            font = meta_font(theme)
            painter.setFont(font)
            painter.setPen(QColor(p.text_muted))
            width = QFontMetrics(font).horizontalAdvance(item.annotation)
            painter.drawText(
                QRectF(right - width, title_mid - 10, width, 20),
                int(Qt.AlignmentFlag.AlignVCenter),
                item.annotation,
            )
            right -= width + 12
        left = rect.left() + 44 + indent
        font = QFont(self._palette.font())
        painter.setFont(font)
        painter.setPen(QColor(p.text if enabled else p.text_muted))
        metrics = QFontMetrics(font)
        painter.drawText(
            QRectF(left, title_mid - 11, max(0.0, right - left), 22),
            int(Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(item.title, Qt.TextElideMode.ElideRight, int(max(0.0, right - left))),
        )
        if two_lines:
            self._snippet(painter, item, QRectF(left, rect.top() + 26, rect.right() - 16 - left, 18))
        elif item.detail:
            painter.setPen(QColor(p.text_muted))
            used = metrics.horizontalAdvance(item.title) + 12
            room = right - left - used
            if room > 40:
                painter.drawText(
                    QRectF(left + used, title_mid - 11, room, 22),
                    int(Qt.AlignmentFlag.AlignVCenter),
                    metrics.elidedText(item.detail, Qt.TextElideMode.ElideRight, int(room)),
                )
        painter.restore()

    def _snippet(self, painter: QPainter, item: PaletteItem, rect: QRectF) -> None:
        p = self._palette.theme.palette
        regular = QFont(self._palette.font())
        regular.setPointSizeF(self._palette.theme.typography.small_pt)
        bold = QFont(regular)
        bold.setWeight(QFont.Weight.DemiBold)
        x = rect.left()
        end = rect.right()
        last = 0
        pieces: list[tuple[str, bool]] = []
        for start, stop in item.marks:
            pieces.append((item.detail[last:start], False))
            pieces.append((item.detail[start:stop], True))
            last = stop
        pieces.append((item.detail[last:], False))
        for text, strong in pieces:
            if not text or x >= end:
                continue
            font = bold if strong else regular
            metrics = QFontMetrics(font)
            width = metrics.horizontalAdvance(text)
            if x + width > end:
                text = metrics.elidedText(text, Qt.TextElideMode.ElideRight, int(end - x))
                width = metrics.horizontalAdvance(text)
            painter.setFont(font)
            painter.setPen(QColor(p.text if strong else p.text_muted))
            painter.drawText(
                QRectF(x, rect.top(), width + 1, rect.height()), int(Qt.AlignmentFlag.AlignVCenter), text
            )
            x += width


class _Footer(QWidget):
    """The prefix legend (each prefix is clickable) and a status line."""

    prefix_chosen = Signal(str)

    def __init__(self, palette: SearchPalette) -> None:
        super().__init__(palette)
        self._palette = palette
        self._regions: list[tuple[QRect, str, str]] = []
        self.status = ""
        self.setMouseTracking(True)
        self.setFixedHeight(34)
        self.setCursor(Qt.CursorShape.ArrowCursor)

    def set_status(self, text: str) -> None:
        self.status = text
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        theme = self._palette.theme
        p = theme.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(p.border), 1))
        painter.drawLine(QPoint(0, 0), QPoint(self.width(), 0))
        meta = meta_font(theme)
        label_font = meta_font(theme)
        metrics = QFontMetrics(label_font)
        x = 14.0
        mid = self.height() / 2 + 1
        self._regions = []
        for prefix, _mode, label, about in PREFIXES:
            start = x
            x += paint_keycaps(painter, x, mid, prefix, meta, p, True) + 5
            painter.setFont(label_font)
            painter.setPen(QColor(p.text_muted))
            width = metrics.horizontalAdvance(label.lower())
            painter.drawText(
                QRectF(x, 0, width + 2, self.height()), int(Qt.AlignmentFlag.AlignVCenter), label.lower()
            )
            x += width + 14
            self._regions.append((QRect(int(start), 0, int(x - start - 8), self.height()), prefix, about))
        if self.status:
            width = metrics.horizontalAdvance(self.status)
            room = self.width() - 14 - x
            if room > 60:
                text = metrics.elidedText(self.status, Qt.TextElideMode.ElideLeft, int(room))
                width = metrics.horizontalAdvance(text)
                painter.drawText(
                    QRectF(self.width() - 14 - width, 0, width + 1, self.height()),
                    int(Qt.AlignmentFlag.AlignVCenter),
                    text,
                )

    def _region_at(self, pos: QPoint) -> tuple[QRect, str, str] | None:
        return next((r for r in self._regions if r[0].contains(pos)), None)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        hit = self._region_at(event.position().toPoint())
        self.setCursor(Qt.CursorShape.PointingHandCursor if hit else Qt.CursorShape.ArrowCursor)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        hit = self._region_at(event.position().toPoint())
        if hit is not None and event.button() == Qt.MouseButton.LeftButton:
            self.prefix_chosen.emit(hit[1])

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip and isinstance(event, QHelpEvent):
            hit = self._region_at(event.pos())
            if hit is not None:
                QToolTip.showText(event.globalPos(), f"Type {hit[1]} : {hit[2]}", self)
            else:
                QToolTip.hideText()
            return True
        return super().event(event)


class _List(QListView):
    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        model = self.model()
        rows = model.rowCount() if model is not None else 0
        height = sum(self.sizeHintForRow(r) for r in range(rows))
        return QSize(400, min(MAX_ROWS_HEIGHT, height + 8))


class SearchPalette(QWidget):
    """A floating panel under the app bar. Enter runs the selected row; Escape or a click elsewhere closes."""

    closed = Signal()

    def __init__(
        self, host: QWidget, theme: Theme, sources: PaletteSources, anchor: Callable[[], int]
    ) -> None:
        super().__init__(host)
        self.setObjectName("Palette")
        self.theme = theme
        self.sources = sources
        self._host = host
        self._anchor = anchor
        self._previous: QWidget | None = None
        self._terms: tuple[str, ...] = ()
        self._hits: SearchResult | None = None
        self._hits_query = ""
        self._tags: list[TagCount] | None = None
        self._skipped: list[SkippedNote] | None = None
        self._mode = NOTES
        self._animation: QVariantAnimation | None = None
        self.answers = 0
        self._enter_pending = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(SHADOW, SHADOW - 6, SHADOW, SHADOW + 4)
        layout.setSpacing(0)
        top = QHBoxLayout()
        top.setContentsMargins(46, 4, 12, 4)
        self.field = QLineEdit()
        self.field.setObjectName("PaletteField")
        self.field.setAccessibleName("Search notes and commands")
        self.field.setAccessibleDescription(
            "Type to search note names and text. Start with > for commands, # for tags, @ for headings, "
            ": for a line number, ? for help."
        )
        self.field.setPlaceholderText("Search notes by name or text")
        top.addWidget(self.field)
        layout.addLayout(top)
        self.list = _List()
        self.list.setObjectName("PaletteList")
        self.list.setAccessibleName("Results")
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setMouseTracking(True)
        self.list.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.list.setVerticalScrollMode(QListView.ScrollMode.ScrollPerPixel)
        self.list.setUniformItemSizes(False)
        self.model = QStandardItemModel(self)
        self.list.setModel(self.model)
        self.list.setItemDelegate(_Delegate(self))
        self.list.clicked.connect(self._run_index)
        layout.addWidget(self.list, 1)
        self.footer = _Footer(self)
        self.footer.prefix_chosen.connect(self.set_prefix)
        layout.addWidget(self.footer)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(SEARCH_DEBOUNCE_MS)
        self._timer.timeout.connect(self._search_index)
        self.field.textChanged.connect(self._on_text)
        self.field.installEventFilter(self)
        host.installEventFilter(self)
        app = QApplication.instance()
        if isinstance(app, QApplication):
            app.focusChanged.connect(self._focus_changed)
        if sources.index is not None:
            sources.index.status_changed.connect(self._index_status)
            sources.index.content_changed.connect(self._index_changed)
        self.hide()

    def apply_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.update()
        self.list.viewport().update()
        self.footer.update()

    def mode(self) -> str:
        return self._mode

    def results(self) -> list[str]:
        return [item.title for item in self.items() if not item.header]

    def items(self) -> list[PaletteItem]:
        found: list[PaletteItem] = []
        for row in range(self.model.rowCount()):
            item = self.model.item(row).data(ITEM_ROLE)
            if isinstance(item, PaletteItem):
                found.append(item)
        return found

    def open(self, text: str = "") -> None:
        if not self.isVisible():
            focus = QApplication.focusWidget()
            self._previous = focus if focus is not None and not self.isAncestorOf(focus) else None
            self._tags = None
            self._skipped = None
        self.field.setText(text)
        self._refresh()
        self._place()
        was_visible = self.isVisible()
        self.show()
        self.raise_()
        self.field.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.field.setCursorPosition(len(text))
        if not was_visible:
            self._animate_in()

    def close_palette(self, restore_focus: bool = True) -> None:
        if not self.isVisible():
            return
        self._timer.stop()
        self.hide()
        previous = self._previous
        self._previous = None
        if restore_focus and previous is not None and shiboken6.isValid(previous) and previous.isVisible():
            previous.setFocus(Qt.FocusReason.PopupFocusReason)
        self.closed.emit()

    def set_prefix(self, prefix: str) -> None:
        _mode, query = parse(self.field.text())
        self.field.setText(prefix + query if prefix != "#" or not query else "#" + query.lstrip("#"))
        self.field.setFocus(Qt.FocusReason.OtherFocusReason)

    def show_skipped(self) -> None:
        index = self.sources.index
        if index is None:
            return
        self._mode = SKIPPED
        index.skipped(self._skipped_ready)

    def run_current(self) -> bool:
        index = self.list.currentIndex()
        if not index.isValid():
            return False
        return self._run_index(index)

    def _run_index(self, index: QModelIndex) -> bool:
        item = index.data(ITEM_ROLE)
        if not isinstance(item, PaletteItem) or item.run is None:
            return False
        if item.kind in ("prefix", "tag"):
            item.run()
            return True
        self.close_palette(restore_focus=item.kind != "note")
        item.run()
        return True

    def _on_text(self, _text: str) -> None:
        self._enter_pending = False
        self._refresh()

    def _refresh(self) -> None:
        text = self.field.text()
        mode, query = parse(text)
        if self._mode == SKIPPED and text == "":
            self._fill(self._skipped_items())
            return
        self._mode = mode
        builders: dict[str, Callable[[str], list[PaletteItem]]] = {
            NOTES: self._note_items,
            COMMANDS: self._command_items,
            TAGS: self._tag_items,
            HEADINGS: self._heading_items,
            LINE: self._line_items,
            HELP: self._help_items,
        }
        self._fill(builders[mode](query))
        if mode == NOTES and query:
            self._timer.start()
        self.footer.set_status(self._status_text())
        self.field.setPlaceholderText(
            {
                NOTES: "Search notes by name or text",
                COMMANDS: "Type a command",
                TAGS: "Type a tag",
                HEADINGS: "Type part of a heading",
                LINE: "Type a line number",
                HELP: "Choose a prefix",
            }[mode]
        )

    def _fill(self, items: list[PaletteItem]) -> None:
        self.model.clear()
        first = -1
        for item in items:
            row = QStandardItem(item.title)
            row.setData(item, ITEM_ROLE)
            row.setEditable(False)
            if item.header:
                row.setFlags(Qt.ItemFlag.NoItemFlags)
            else:
                row.setToolTip(item.detail if item.kind != "note" else "")
                if first < 0 and item.run is not None:
                    first = self.model.rowCount()
            self.model.appendRow(row)
        if first >= 0:
            self.list.setCurrentIndex(self.model.index(first, 0))
        self.list.updateGeometry()
        if self.isVisible():
            self._place()

    def _note_row(self, path: str, annotation: str = "") -> PaletteItem:
        roots = self.sources.roots()
        return PaletteItem(
            note_title(path),
            "note",
            "note",
            location_of(path, roots),
            annotation=annotation,
            run=partial(self._open, path),
        )

    def _note_items(self, query: str) -> list[PaletteItem]:
        opened = self.sources.open_notes()
        recent = [
            p for p in self.sources.recent_notes() if p.casefold() not in {o.casefold() for o in opened}
        ]
        items: list[PaletteItem] = []
        if not query:
            self._terms = ()
            if opened:
                items.append(PaletteItem("Open tabs", header=True))
                items += [self._note_row(p) for p in opened]
            if recent:
                items.append(PaletteItem("Recent", header=True))
                items += [self._note_row(p) for p in recent[:8]]
            if not items:
                items.append(PaletteItem("Type to search every note in your library", "hint", "search"))
            return items
        local = [p for p in [*opened, *recent] if _name_match(query, p)]
        shown = {p.casefold() for p in local}
        if local:
            items.append(PaletteItem("Open and recent", header=True))
            items += [self._note_row(p) for p in local[:6]]
        hits = self._hits if self._hits_query == query else None
        if hits is not None:
            rows = [h for h in hits.hits if h.path.casefold() not in shown]
            if rows:
                items.append(PaletteItem("In your library", header=True))
                roots = self.sources.roots()
                for hit in rows[:40]:
                    detail = hit.snippet if hit.snippet.strip() else location_of(hit.path, roots)
                    items.append(
                        PaletteItem(
                            hit.title,
                            "note",
                            "note",
                            detail,
                            hit.marks if hit.snippet.strip() else (),
                            annotation=location_of(hit.path, roots),
                            run=partial(self._open, hit.path),
                        )
                    )
            elif not local:
                items.append(PaletteItem(hits.problem or "No notes contain that", "hint", "search"))
        elif self.sources.index is None and not local:
            items.append(PaletteItem("Search is not available", "hint", "search"))
        items.append(
            PaletteItem(
                f"Search commands for \N{LEFT DOUBLE QUOTATION MARK}{query}\N{RIGHT DOUBLE QUOTATION MARK}",
                "prefix",
                "command",
                shortcut=">",
                run=partial(self.field.setText, ">" + query),
            )
        )
        return items

    def _open(self, path: str) -> None:
        self.sources.open_note(path, self._terms)

    def _search_index(self) -> None:
        mode, query = parse(self.field.text())
        index = self.sources.index
        if mode != NOTES or not query or index is None:
            return
        compiled = compile_query(query)
        terms = compiled.terms if compiled is not None else ()
        if compiled is not None and not compiled.terms and compiled.tags:
            terms = tuple("#" + t for t in compiled.tags)
        index.search(query, partial(self._answer, query, terms))

    def _answer(self, query: str, terms: tuple[str, ...], result: SearchResult) -> None:
        self.answers += 1
        self._hits = result
        self._hits_query = query
        self._terms = terms
        mode, current = parse(self.field.text())
        if mode == NOTES and current == query:
            keep = self.list.currentIndex().row()
            self._fill(self._note_items(query))
            if self._enter_pending:
                self._enter_pending = False
                self.run_current()
            elif 0 < keep < self.model.rowCount():
                self.list.setCurrentIndex(self.model.index(keep, 0))

    def _command_items(self, query: str) -> list[PaletteItem]:
        items: list[PaletteItem] = []
        for command in self.sources.registry.search(query, limit=60):
            if command.category == "Notes":
                continue
            items.append(
                PaletteItem(
                    command.title,
                    "command",
                    "command",
                    annotation="" if command.category == "Commands" else command.category,
                    shortcut=command.shortcut,
                    run=command.run,
                )
            )
        if not items:
            items.append(PaletteItem("No command matches", "hint", "command"))
        return items

    def _tag_items(self, query: str) -> list[PaletteItem]:
        index = self.sources.index
        if index is None:
            return [PaletteItem("Tags are not available", "hint", "tag")]
        if self._tags is None:
            index.tags(self._tags_ready)
            return [PaletteItem("Reading tags", "hint", "tag")]
        wanted = query.casefold()
        items = [
            PaletteItem(
                f"#{tag.display}",
                "tag",
                "tag",
                annotation=f"{tag.count:,} note{'s' if tag.count != 1 else ''}",
                run=partial(self.field.setText, f"#{tag.display} "),
            )
            for tag in sorted(self._tags, key=lambda t: (-t.count, t.key))
            if not wanted or wanted in tag.key
        ]
        if not items:
            items.append(
                PaletteItem(
                    "No tags yet. Write #tag in a note to tag it." if not self._tags else "No tag matches",
                    "hint",
                    "tag",
                )
            )
        return items

    def _tags_ready(self, tags: list[TagCount]) -> None:
        self._tags = tags
        if self._mode == TAGS:
            self._refresh()

    def _heading_items(self, query: str) -> list[PaletteItem]:
        outline = self.sources.outline()
        if outline is None:
            return [PaletteItem("Open a note to jump to its headings", "hint", "heading")]
        if outline.too_large:
            return [PaletteItem("This note is too large for an outline", "hint", "heading")]
        wanted = query.casefold()
        items = [
            PaletteItem(
                heading.text or "(empty heading)",
                "heading",
                "heading",
                annotation=f"H{heading.level}  {heading.line + 1}",
                level=max(0, heading.level - 1),
                run=partial(self.sources.reveal_heading, heading.line),
            )
            for heading in outline.headings
            if not wanted or wanted in heading.text.casefold()
        ]
        if not items:
            text = "No headings in this note. Lines starting with # become headings."
            items.append(
                PaletteItem(text if not outline.headings else "No heading matches", "hint", "heading")
            )
        return items

    def _line_items(self, query: str) -> list[PaletteItem]:
        total = self.sources.line_count()
        if total is None:
            return [PaletteItem("Open a note to go to a line", "hint", "line")]
        if not query.isdigit():
            return [PaletteItem(f"Type a line number from 1 to {total:,}", "hint", "line")]
        line = max(1, min(total, int(query)))
        return [
            PaletteItem(
                f"Go to line {line:,}",
                "line",
                "line",
                annotation=f"of {total:,}",
                run=partial(self.sources.go_to_line, line),
            )
        ]

    def _help_items(self, _query: str) -> list[PaletteItem]:
        items = [
            PaletteItem(
                "Notes",
                "prefix",
                "search",
                "type words to search names and text",
                shortcut="",
                run=partial(self.field.setText, ""),
            )
        ]
        for prefix, mode, label, about in PREFIXES:
            if mode == HELP:
                continue
            items.append(
                PaletteItem(
                    label,
                    "prefix",
                    _GLYPHS[mode],
                    about,
                    shortcut=prefix,
                    run=partial(self.field.setText, prefix),
                )
            )
        return items

    def _skipped_ready(self, notes: list[SkippedNote]) -> None:
        self._skipped = notes
        if self.isVisible():
            self.field.blockSignals(True)
            self.field.clear()
            self.field.blockSignals(False)
            self._mode = SKIPPED
            self._fill(self._skipped_items())

    def _skipped_items(self) -> list[PaletteItem]:
        notes = self._skipped or []
        items = [PaletteItem("Listed by name only; their text is not searched", header=True)]
        items += [
            PaletteItem(
                note_title(n.path),
                "note",
                "note",
                annotation=n.reason,
                run=partial(self.sources.open_note, n.path, ()),
            )
            for n in notes
        ]
        return items

    def _status_text(self) -> str:
        index = self.sources.index
        if self._mode == COMMANDS:
            return f"{len(self.sources.registry.all()):,} commands"
        if index is None:
            return ""
        status = index.status
        if status.state is IndexState.INDEXING:
            return f"indexing, {status.pending:,} left" if status.pending else "indexing"
        if status.state is IndexState.ERROR:
            return "search unavailable"
        text = f"{status.indexed:,} notes"
        if status.skipped:
            text += f", {status.skipped:,} not searched"
        return text

    def _index_status(self, _status: IndexStatus) -> None:
        if self.isVisible():
            self.footer.set_status(self._status_text())

    def _index_changed(self) -> None:
        self._tags = None
        if not self.isVisible():
            return
        if self._mode == TAGS:
            self._refresh()
        elif self._mode == NOTES and parse(self.field.text())[1]:
            self._timer.start()

    def _move(self, step: int) -> None:
        count = self.model.rowCount()
        if not count:
            return
        row = self.list.currentIndex().row()
        for _ in range(count):
            row = max(0, min(count - 1, row + step))
            item = self.model.item(row).data(ITEM_ROLE)
            if isinstance(item, PaletteItem) and not item.header:
                self.list.setCurrentIndex(self.model.index(row, 0))
                return
            if row in (0, count - 1):
                return

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self._host and event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        if watched is self.field and event.type() == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            key = event.key()
            if key in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                self._move(1 if key == Qt.Key.Key_Down else -1)
                return True
            if key in (Qt.Key.Key_PageDown, Qt.Key.Key_PageUp):
                for _ in range(8):
                    self._move(1 if key == Qt.Key.Key_PageDown else -1)
                return True
            if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
                self._timer.stop()
                query = parse(self.field.text())[1]
                if (
                    self._mode == NOTES
                    and query
                    and self._hits_query != query
                    and self.sources.index is not None
                ):
                    self._enter_pending = True
                    self._search_index()
                    return True
                self.run_current()
                return True
            if key == Qt.Key.Key_Escape:
                self.close_palette()
                return True
            if key == Qt.Key.Key_Tab:
                current = self.list.currentIndex().data(ITEM_ROLE)
                if (
                    isinstance(current, PaletteItem)
                    and current.kind in ("tag", "prefix")
                    and current.run is not None
                ):
                    current.run()
                return True
        return False

    def _focus_changed(self, _old: QWidget | None, new: QWidget | None) -> None:
        if self.isVisible() and new is not None and new is not self and not self.isAncestorOf(new):
            self.close_palette(restore_focus=False)

    def _place(self) -> None:
        host = self._host
        width = min(self.theme.metrics.palette_width, host.width() - 48) + 2 * SHADOW
        self.list.setMinimumHeight(0)
        list_height = self.list.sizeHint().height()
        height = SHADOW - 6 + 48 + list_height + 34 + SHADOW + 4
        top = self._anchor() - (SHADOW - 6) + 6
        height = min(height, host.height() - top - 16)
        self.setGeometry((host.width() - width) // 2, top, width, max(120, height))

    def _animate_in(self) -> None:
        duration = self.theme.ms(self.theme.motion.palette_ms)
        if duration <= 0:
            return
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(0.0)
        self.setGraphicsEffect(effect)
        final = self.pos()
        animation = QVariantAnimation(self)
        animation.setStartValue(0.0)
        animation.setEndValue(1.0)
        animation.setDuration(duration)
        animation.setEasingCurve(QEasingCurve.Type.OutCubic)

        def step(value: object) -> None:
            progress = float(value)  # type: ignore[arg-type]
            effect.setOpacity(progress)
            self.move(final.x(), final.y() - round(6 * (1 - progress)))

        def done() -> None:
            self.move(final)
            self.setGraphicsEffect(None)  # type: ignore[arg-type]

        animation.valueChanged.connect(step)
        animation.finished.connect(done)
        self._animation = animation
        animation.start(QVariantAnimation.DeletionPolicy.DeleteWhenStopped)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        t = self.theme
        p = t.palette
        painter = QPainter(self)
        panel = QRectF(self.rect()).adjusted(SHADOW, SHADOW - 6, -SHADOW, -SHADOW - 4)
        paint_shadow(painter, panel, t.radius.lg, p.shadow, depth=SHADOW, strength=0.55 if t.dark else 0.28)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(p.border_strong if t.dark else p.border), 1))
        painter.setBrush(QColor(p.surface))
        painter.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), t.radius.lg, t.radius.lg)
        glyph = _GLYPHS.get(self._mode, "search")
        draw_glyph(
            painter, glyph, QRectF(panel.left() + 17, panel.top() + 16, 18, 18), QColor(p.text_secondary)
        )
        painter.setPen(QPen(QColor(p.border), 1))
        y = panel.top() + 50.5
        painter.drawLine(QPoint(int(panel.left()), int(y)), QPoint(int(panel.right()), int(y)))


_GLYPHS = {
    NOTES: "search",
    COMMANDS: "command",
    TAGS: "tag",
    HEADINGS: "heading",
    LINE: "line",
    HELP: "help",
    SKIPPED: "info",
}
