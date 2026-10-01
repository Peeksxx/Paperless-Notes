"""Home: shown when no note is open and from the Home button (Alt+Home).

It lists notes to continue, what changed in the library most recently (on any PC), pinned notes, the most
used tags and the library folders, and shows how to reach search. Everything is read from local state and
the search index; nothing here scans the disk on the UI thread.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from PySide6.QtCore import QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QEnterEvent, QFont, QFontMetrics, QPainter, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import (
    QAbstractButton,
    QBoxLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core.search_index import NoteInfo, Overview
from paperless_notes.ui.shell.flow import FlowLayout
from paperless_notes.ui.shell.labels import day_group, location_of, note_title, relative_time
from paperless_notes.ui.shell.motion import Glow, faded, mix
from paperless_notes.ui.shell.widgets import (
    RowButton,
    SectionLabel,
    caption_font,
    keycaps_width,
    meta_font,
    paint_keycaps,
)
from paperless_notes.ui.theme.icons import draw_glyph
from paperless_notes.ui.theme.tokens import Theme

TILE = QSize(212, 108)
WEEK_S = 7 * 86400
_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def greeting(hour: int) -> str:
    if 5 <= hour < 12:
        return "Good morning"
    if 12 <= hour < 18:
        return "Good afternoon"
    return "Good evening"


def date_line(now: float) -> str:
    local = time.localtime(now)
    return f"{_DAYS[local.tm_wday]} {local.tm_mday} {_MONTHS[local.tm_mon - 1]}"


class _Hoverable(QAbstractButton):
    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._theme = theme
        self._hover = False
        self._glow = Glow(self, lambda: self._theme.ms(self._theme.motion.fast_ms))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def enterEvent(self, event: QEnterEvent) -> None:  # noqa: N802 - Qt override
        self._hover = True
        self._glow.set_on(True)
        super().enterEvent(event)

    def leaveEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        self._hover = False
        self._glow.set_on(False)
        super().leaveEvent(event)  # type: ignore[arg-type]

    def _focus_ring(self, painter: QPainter, rect: QRectF, radius: float) -> None:
        if self.hasFocus() and self.window().testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange):
            painter.setPen(QPen(QColor(self._theme.palette.focus), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), radius, radius)


class NoteTile(_Hoverable):
    """A recently opened note: folder, title on up to two lines, and when it last changed."""

    def __init__(self, path: str, location: str, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(theme, parent)
        self.path = path
        self.location = location
        self.when = ""
        self.setText(note_title(path))
        self.setAccessibleName(f"Open {note_title(path)}")
        self.setToolTip(path)
        self.setFixedSize(TILE)

    def set_when(self, text: str) -> None:
        self.when = text
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        t = self._theme
        p = t.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(mix(p.border, p.border_strong, self._glow.amount), 1))
        painter.setBrush(QColor(p.hover if self.isDown() else p.surface if t.dark else p.window))
        painter.drawRoundedRect(rect, t.radius.lg, t.radius.lg)
        self._focus_ring(painter, rect, t.radius.lg)
        draw_glyph(painter, "note", QRectF(14, 14, 14, 14), QColor(p.text_muted))
        meta = meta_font(t)
        painter.setFont(meta)
        painter.setPen(QColor(p.text_muted))
        metrics = QFontMetrics(meta)
        painter.drawText(
            QRectF(34, 10, self.width() - 48, 22),
            int(Qt.AlignmentFlag.AlignVCenter),
            metrics.elidedText(self.location, Qt.TextElideMode.ElideMiddle, self.width() - 48),
        )
        title = QFont(self.font())
        title.setWeight(QFont.Weight.DemiBold)
        title.setPointSizeF(t.typography.ui_pt + 1)
        painter.setFont(title)
        painter.setPen(QColor(p.text))
        tm = QFontMetrics(title)
        width = self.width() - 28
        words = self.text().split(" ")
        lines: list[str] = []
        current = ""
        for word in words:
            trial = f"{current} {word}".strip()
            if tm.horizontalAdvance(trial) <= width or not current:
                current = trial
            else:
                lines.append(current)
                current = word
        lines.append(current)
        if len(lines) > 2:
            lines = [lines[0], " ".join(lines[1:])]
        for row, line in enumerate(lines[:2]):
            painter.drawText(
                QRectF(14, 40 + row * (tm.height() + 1), width, tm.height()),
                int(Qt.AlignmentFlag.AlignVCenter),
                tm.elidedText(line, Qt.TextElideMode.ElideRight, width),
            )
        if self.when:
            painter.setFont(meta)
            painter.setPen(QColor(p.text_muted))
            painter.drawText(
                QRectF(14, self.height() - 26, width, 18), int(Qt.AlignmentFlag.AlignVCenter), self.when
            )


class ChangeRow(_Hoverable):
    """One entry on the recent-changes rail."""

    RAIL_X = 9

    def __init__(self, info: NoteInfo, location: str, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(theme, parent)
        self.path = info.path
        self.info = info
        self.location = location
        self.setText(info.title)
        self.setAccessibleName(f"Open {info.title}")
        self.setToolTip(info.path)
        self.setFixedHeight(34)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        t = self._theme
        p = t.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = QRectF(self.rect()).adjusted(22, 1, 0, -1)
        glow = 1.0 if self.isDown() else self._glow.amount
        if glow > 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(faded(p.hover, glow))
            painter.drawRoundedRect(body, t.radius.md, t.radius.md)
        self._focus_ring(painter, body, t.radius.md)
        mid = self.height() / 2
        painter.setPen(QPen(mix(p.text_muted, p.text, glow), 1.5))
        painter.setBrush(QColor(p.page))
        painter.drawEllipse(QPointF(self.RAIL_X + 0.5, mid), 3.5, 3.5)
        meta = meta_font(t)
        stamp = time.strftime("%H:%M", time.localtime(self.info.mtime_ns / 1e9))
        label = relative_time(self.info.mtime_ns)
        when = stamp if day_group(self.info.mtime_ns) == "Today" else label
        mm = QFontMetrics(meta)
        when_w = mm.horizontalAdvance(when)
        right = self.width() - 10
        painter.setFont(meta)
        painter.setPen(QColor(p.text_muted))
        painter.drawText(
            QRectF(right - when_w, 0, when_w, self.height()), int(Qt.AlignmentFlag.AlignVCenter), when
        )
        right -= when_w + 16
        fm = self.fontMetrics()
        title_w = min(fm.horizontalAdvance(self.text()) + 4, int((right - 34) * 0.6))
        painter.setFont(self.font())
        painter.setPen(QColor(p.text))
        painter.drawText(
            QRectF(34, 0, title_w + 4, self.height()),
            int(Qt.AlignmentFlag.AlignVCenter),
            fm.elidedText(self.text(), Qt.TextElideMode.ElideRight, title_w),
        )
        left = 34 + title_w + 12
        if right - left > 30:
            painter.setFont(meta)
            painter.setPen(QColor(p.text_muted))
            painter.drawText(
                QRectF(left, 0, right - left, self.height()),
                int(Qt.AlignmentFlag.AlignVCenter),
                mm.elidedText(self.location, Qt.TextElideMode.ElideMiddle, int(right - left)),
            )


class _DayLabel(QWidget):
    def __init__(self, text: str, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._text = text
        self._theme = theme
        self.setAccessibleName(text)
        self.setFixedHeight(28)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        p = self._theme.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(p.text_secondary))
        painter.drawRoundedRect(QRectF(ChangeRow.RAIL_X - 2.5, self.height() / 2 - 2.5, 6, 6), 1.5, 1.5)
        painter.setFont(caption_font(self._theme))
        painter.setPen(QColor(p.text_secondary))
        painter.drawText(
            QRectF(34, 0, self.width() - 34, self.height()), int(Qt.AlignmentFlag.AlignVCenter), self._text
        )


class ChangeRail(QWidget):
    """Recently changed notes grouped by day along a vertical rail, newest first."""

    open_requested = Signal(str)

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self._theme = theme
        self.rows: list[ChangeRow] = []
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.empty = QLabel("Notes you or your other PCs change show up here.")
        self.empty.setProperty("role", "muted")
        self.empty.setWordWrap(True)
        self._layout.addWidget(self.empty)

    def set_changes(self, notes: list[NoteInfo], roots: list[str]) -> None:
        while self._layout.count() > 1:
            item = self._layout.takeAt(1)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.deleteLater()
        self.rows = []
        group = ""
        for info in notes:
            label = day_group(info.mtime_ns)
            if label != group:
                group = label
                self._layout.addWidget(_DayLabel(label, self._theme))
            row = ChangeRow(info, location_of(info.path, roots), self._theme)
            row.clicked.connect(lambda _c=False, p=info.path: self.open_requested.emit(p))
            self._layout.addWidget(row)
            self.rows.append(row)
        self.empty.setVisible(not notes)
        self.update()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        for index in range(self._layout.count()):
            item = self._layout.itemAt(index)
            widget = item.widget() if item is not None else None
            if isinstance(widget, (ChangeRow, _DayLabel)):
                widget.apply_theme(theme)
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        if not self.rows:
            return
        painter = QPainter(self)
        painter.setPen(QPen(QColor(self._theme.palette.marker), 1))
        top = 14
        bottom = self.rows[-1].geometry().center().y()
        x = ChangeRow.RAIL_X + 0.5
        painter.drawLine(QPointF(x, top), QPointF(x, bottom))


class TagChip(_Hoverable):
    def __init__(self, display: str, count: int, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(theme, parent)
        self.display = display
        self.count = count
        self.setText(f"#{display}")
        self.setAccessibleName(f"Notes tagged #{display}, {count}")
        self.setToolTip(f"{count:,} note{'s' if count != 1 else ''} tagged #{display}")

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        meta = QFontMetrics(meta_font(self._theme))
        return QSize(
            self.fontMetrics().horizontalAdvance(self.text()) + meta.horizontalAdvance(str(self.count)) + 28,
            28,
        )

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        t = self._theme
        p = t.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        glow = self._glow.amount
        painter.setPen(QPen(mix(p.border, p.border_strong, glow), 1))
        painter.setBrush(mix(p.page, p.hover, glow))
        painter.drawRoundedRect(rect, t.radius.sm, t.radius.sm)
        self._focus_ring(painter, rect, t.radius.sm)
        fm = self.fontMetrics()
        painter.setFont(self.font())
        painter.setPen(QColor(p.text))
        text_w = fm.horizontalAdvance(self.text())
        painter.drawText(
            QRectF(10, 0, text_w + 1, self.height()), int(Qt.AlignmentFlag.AlignVCenter), self.text()
        )
        painter.setFont(meta_font(t))
        painter.setPen(QColor(p.text_muted))
        painter.drawText(
            QRectF(10 + text_w + 7, 0, self.width(), self.height()),
            int(Qt.AlignmentFlag.AlignVCenter),
            str(self.count),
        )


class TipLine(QWidget):
    """Search hint with the shortcut and prefixes drawn as keycaps."""

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    PARTS: tuple[tuple[str, str], ...] = (
        ("Ctrl+Shift+P", "searches your notes."),
        (">", "commands"),
        ("#", "tags"),
        ("@", "headings"),
        (":", "line"),
    )

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self._theme = theme
        self.setFixedHeight(30)
        self.setAccessibleName(
            "Press Ctrl+Shift+P to search your notes. Type > for commands, # for tags, @ for headings, "
            ": for a line."
        )

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        t = self._theme
        p = t.palette
        painter = QPainter(self)
        meta = meta_font(t)
        fm = self.fontMetrics()
        x = 0.0
        mid = self.height() / 2
        painter.setPen(QColor(p.text_muted))
        painter.setFont(self.font())
        lead = "Press"
        painter.drawText(
            QRectF(x, 0, fm.horizontalAdvance(lead) + 2, self.height()),
            int(Qt.AlignmentFlag.AlignVCenter),
            lead,
        )
        x += fm.horizontalAdvance(lead) + 8
        for index, (keys, words) in enumerate(self.PARTS):
            if index == 1:
                then = "Start with"
                painter.setFont(self.font())
                painter.setPen(QColor(p.text_muted))
                painter.drawText(
                    QRectF(x, 0, fm.horizontalAdvance(then) + 2, self.height()),
                    int(Qt.AlignmentFlag.AlignVCenter),
                    then,
                )
                x += fm.horizontalAdvance(then) + 8
            if x + keycaps_width(keys, meta) > self.width():
                return
            x += paint_keycaps(painter, x, mid, keys, meta, p, True) + 6
            painter.setFont(self.font())
            painter.setPen(QColor(p.text_muted))
            width = fm.horizontalAdvance(words)
            if x + width > self.width():
                return
            painter.drawText(
                QRectF(x, 0, width + 2, self.height()), int(Qt.AlignmentFlag.AlignVCenter), words
            )
            x += width + (14 if index else 12)


class Dashboard(QScrollArea):
    new_note_requested = Signal()
    open_file_requested = Signal()
    open_requested = Signal(str)
    search_requested = Signal(str)
    folder_requested = Signal(str)
    NARROW = 820

    def __init__(self, theme: Theme, synced: Callable[[str], bool] | None = None) -> None:
        super().__init__()
        self.setObjectName("DashboardScroll")
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._theme = theme
        self._synced = synced or (lambda _path: False)
        self._roots: list[str] = []
        self._recent: list[str] = []
        self._pinned: list[str] = []
        self._overview: Overview | None = None
        body = QWidget()
        body.setObjectName("DashboardBody")
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWidget(body)
        outer = QHBoxLayout(body)
        outer.setContentsMargins(0, 0, 0, 0)
        column = QWidget()
        column.setMaximumWidth(theme.metrics.dashboard_width)
        outer.addStretch(1)
        outer.addWidget(column, 12)
        outer.addStretch(1)
        s = theme.spacing
        layout = QVBoxLayout(column)
        layout.setContentsMargins(s.xxxl + s.md, s.xxxl + s.xl, s.xxxl + s.md, s.xxxl)
        layout.setSpacing(0)
        self.date = QLabel("")
        self.date.setObjectName("HomeDate")
        self.date.setAccessibleName("Today")
        self.heading = QLabel("")
        self.heading.setObjectName("HomeHeading")
        self.heading.setAccessibleName("Home")
        self.stats = QLabel("Reading your library")
        self.stats.setProperty("role", "meta")
        layout.addWidget(self.date)
        layout.addSpacing(s.xs)
        layout.addWidget(self.heading)
        layout.addSpacing(s.sm)
        layout.addWidget(self.stats)
        layout.addSpacing(s.xxl)
        actions = FlowLayout(spacing=s.md)
        self.new_button = RowButton("New note", "new_note", theme, "Ctrl+N", framed=True)
        self.new_button.setToolTip("Create a note in the selected library folder (Ctrl+N)")
        self.open_button = RowButton("Open file", "open", theme, "Ctrl+O", framed=True)
        self.open_button.setToolTip("Open a Markdown or text file from this PC (Ctrl+O)")
        self.search_button = RowButton("Search", "search", theme, "Ctrl+Shift+P", framed=True)
        self.search_button.setToolTip("Search note names and text; type > for commands (Ctrl+Shift+P)")
        self.new_button.clicked.connect(self.new_note_requested.emit)
        self.open_button.clicked.connect(self.open_file_requested.emit)
        self.search_button.clicked.connect(lambda: self.search_requested.emit(""))
        self.action_buttons = [self.new_button, self.open_button, self.search_button]
        for button in self.action_buttons:
            button.setObjectName("ActionButton")
            button.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            button.setFixedWidth(button.sizeHint().width() + 16)
            button.setFixedHeight(36)
            actions.addWidget(button)
        layout.addLayout(actions)
        layout.addSpacing(s.xxxl)
        self.continue_label = SectionLabel("Continue", theme)
        layout.addWidget(self.continue_label)
        layout.addSpacing(s.md)
        self.tiles_host = QWidget()
        self.tiles_layout = FlowLayout(self.tiles_host, spacing=s.lg)
        self.tiles: list[NoteTile] = []
        layout.addWidget(self.tiles_host)
        self.recent_empty = QLabel("Notes you open appear here. Create one with New note, or open a file.")
        self.recent_empty.setProperty("role", "muted")
        self.recent_empty.setWordWrap(True)
        layout.addWidget(self.recent_empty)
        layout.addSpacing(s.xxxl)
        self.lower_row = QBoxLayout(QBoxLayout.Direction.LeftToRight)
        self.lower_row.setSpacing(s.xxxl + s.md)
        left = QVBoxLayout()
        left.setSpacing(s.md)
        self.changes_label = SectionLabel("Recent changes", theme)
        self.rail = ChangeRail(theme)
        self.rail.open_requested.connect(self.open_requested.emit)
        left.addWidget(self.changes_label)
        left.addWidget(self.rail)
        left.addStretch(1)
        right = QVBoxLayout()
        right.setSpacing(s.md)
        self.pinned_label = SectionLabel("Pinned", theme)
        self.pinned_host = QVBoxLayout()
        self.pinned_host.setSpacing(0)
        self.pinned_rows: list[RowButton] = []
        self.pinned_empty = QLabel("Pin a tab from its right-click menu to keep it here.")
        self.pinned_empty.setProperty("role", "muted")
        self.pinned_empty.setWordWrap(True)
        self.tags_label = SectionLabel("Tags", theme)
        self.tags_host = QWidget()
        self.tags_layout = FlowLayout(self.tags_host, spacing=s.sm)
        self.tag_chips: list[TagChip] = []
        self.tags_empty = QLabel("Write #tag anywhere in a note to tag it.")
        self.tags_empty.setProperty("role", "muted")
        self.tags_empty.setWordWrap(True)
        self.library_label = SectionLabel("Library", theme)
        self.library_host = QVBoxLayout()
        self.library_host.setSpacing(0)
        self.library_rows: list[RowButton] = []
        right.addWidget(self.pinned_label)
        right.addLayout(self.pinned_host)
        right.addWidget(self.pinned_empty)
        right.addSpacing(s.xl)
        right.addWidget(self.tags_label)
        right.addWidget(self.tags_host)
        right.addWidget(self.tags_empty)
        right.addSpacing(s.xl)
        right.addWidget(self.library_label)
        right.addLayout(self.library_host)
        right.addStretch(1)
        self.lower_row.addLayout(left, 3)
        self.lower_row.addLayout(right, 2)
        layout.addLayout(self.lower_row)
        layout.addSpacing(s.xxxl)
        self.tip = TipLine(theme)
        layout.addWidget(self.tip)
        layout.addStretch(1)
        self.apply_theme(theme)
        self.refresh_clock()

    def refresh_clock(self) -> None:
        now = time.time()
        self.date.setText(date_line(now).upper())
        self.heading.setText(greeting(time.localtime(now).tm_hour))

    def section_tops(self, target: QWidget) -> list[int]:
        """Where Home's sections start in ``target``'s coordinates: the heading block, Continue, and the
        lower row, each with the space above it."""
        gap = self._theme.spacing.xl
        tops = [0]
        for label in (self.continue_label, self.changes_label):
            if label.isVisible():
                tops.append(label.mapTo(target, QPoint(0, 0)).y() - gap)
        return tops

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        themed: list[RowButton | NoteTile | TagChip | SectionLabel | ChangeRail | TipLine] = [
            *self.action_buttons,
            *self.tiles,
            *self.pinned_rows,
            *self.tag_chips,
            *self.library_rows,
            self.continue_label,
            self.changes_label,
            self.pinned_label,
            self.tags_label,
            self.library_label,
            self.rail,
            self.tip,
        ]
        for widget in themed:
            widget.apply_theme(theme)

    def set_roots(self, roots: list[str]) -> None:
        self._roots = list(roots)
        self._clear(self.library_host, self.library_rows)
        for root in roots:
            name = root.rstrip("\\").split("\\")[-1] or root
            row = RowButton(
                name, "folder", self._theme, annotation="ONEDRIVE" if self._synced(root) else "THIS PC"
            )
            row.setToolTip(f"{root}\nShow it in the sidebar")
            row.clicked.connect(lambda _c=False, r=root: self.folder_requested.emit(r))
            self.library_host.addWidget(row)
            self.library_rows.append(row)
        self._render()

    def set_notes(self, pinned: list[str], recent: list[str]) -> None:
        self._pinned = list(pinned)
        self._recent = list(recent)
        self._render()

    def set_overview(self, overview: Overview) -> None:
        self._overview = overview
        self._render()

    def recent_paths(self) -> list[str]:
        return [tile.path for tile in self.tiles]

    @staticmethod
    def _clear(host: QVBoxLayout | FlowLayout, rows: list) -> None:  # type: ignore[type-arg]
        for widget in rows:
            host.removeWidget(widget)
            widget.deleteLater()
        rows.clear()

    def _render(self) -> None:
        self.refresh_clock()
        overview = self._overview
        known = {info.path.casefold(): info for info in overview.known} if overview is not None else {}
        self._clear(self.tiles_layout, self.tiles)
        for path in self._recent[:8]:
            tile = NoteTile(path, location_of(path, self._roots), self._theme)
            info = known.get(path.casefold())
            if info is not None:
                tile.set_when(relative_time(info.mtime_ns))
            tile.clicked.connect(lambda _c=False, p=path: self.open_requested.emit(p))
            self.tiles_layout.addWidget(tile)
            self.tiles.append(tile)
        self.tiles_host.setVisible(bool(self.tiles))
        self.recent_empty.setVisible(not self.tiles)
        self._clear(self.pinned_host, self.pinned_rows)
        for path in self._pinned:
            row = RowButton(
                note_title(path), "pin", self._theme, annotation=location_of(path, self._roots).upper()
            )
            row.setToolTip(path)
            row.clicked.connect(lambda _c=False, p=path: self.open_requested.emit(p))
            self.pinned_host.addWidget(row)
            self.pinned_rows.append(row)
        self.pinned_empty.setVisible(not self._pinned)
        self._clear(self.tags_layout, self.tag_chips)
        if overview is not None:
            for tag in overview.tags:
                chip = TagChip(tag.display, tag.count, self._theme)
                chip.clicked.connect(lambda _c=False, d=tag.display: self.search_requested.emit(f"#{d} "))
                self.tags_layout.addWidget(chip)
                self.tag_chips.append(chip)
            self.rail.set_changes(list(overview.recent[:10]), self._roots)
            folders = len(self._roots)
            parts = [
                f"{overview.indexed:,} note{'s' if overview.indexed != 1 else ''}",
                f"{folders} folder{'s' if folders != 1 else ''}",
                f"{overview.changed_since:,} changed this week",
            ]
            self.stats.setText("  \N{MIDDLE DOT}  ".join(parts))
        self.tags_host.setVisible(bool(self.tag_chips))
        self.tags_empty.setVisible(not self.tag_chips)
        self.tiles_host.updateGeometry()
        self.tags_host.updateGeometry()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        narrow = self.viewport().width() < self.NARROW
        direction = QBoxLayout.Direction.TopToBottom if narrow else QBoxLayout.Direction.LeftToRight
        if self.lower_row.direction() != direction:
            self.lower_row.setDirection(direction)
