"""The tab strip: painted tabs that merge into the page, a state dot for unsaved or troubled notes, a graphite
marker on the active tab, close buttons on the active and hovered tabs, pins, reorder, an overflow list and
a context menu that shows shortcuts. Unpinned tabs share one width, like a browser: it shrinks as tabs are
added and grows as they close, between MIN_TAB and MAX_TAB, and a new tab grows in.

Tab text keeps the state mark (a bullet for unsaved, ! for a problem, ? for missing) for tooltips and screen
readers; the painted tab shows the mark as a coloured dot instead."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QEvent, QPoint, QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QIcon,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QMenu, QStyle, QStyleOptionTab, QTabBar, QToolButton, QWidget

from paperless_notes.ui.shell.motion import Tween
from paperless_notes.ui.shell.widgets import ATTENTION, BUSY, paint_dot
from paperless_notes.ui.theme.icons import draw_glyph, glyph_icon
from paperless_notes.ui.theme.tokens import Theme

MARKS = {" \N{BULLET}": BUSY, " !": ATTENTION, " ?": ATTENTION}
CLOSE_ROOM = 24
MAX_TAB = 248
MIN_TAB = 56
PINNED_MIN = 72


def split_mark(text: str) -> tuple[str, str]:
    """The visible name and the state tone carried by a trailing mark."""
    for mark, tone in MARKS.items():
        if text.endswith(mark):
            return text[: -len(mark)], tone
    return text, ""


class TabStrip(QTabBar):
    close_requested = Signal(int)
    pin_toggled = Signal(int)
    close_others_requested = Signal(int)
    reopen_requested = Signal()
    copy_path_requested = Signal(int)
    history_requested = Signal(int)

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("TabStrip")
        self.setAccessibleName("Open notes")
        self.setMovable(True)
        self.setTabsClosable(False)
        self.setDocumentMode(True)
        self.setExpanding(False)
        self.setUsesScrollButtons(True)
        # paint_tab elides the name to its own layout; Qt's elision assumes its icon spacing and cut pinned
        # names short ("Sourd..." in a tab that fits "Sourdough").
        self.setElideMode(Qt.TextElideMode.ElideNone)
        self.setDrawBase(False)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._context_menu)
        self.currentChanged.connect(lambda _i: self._sync_close_buttons())
        self._theme = theme
        self._hover = -1
        self._pane_active = True
        self._pin_icon = QIcon()
        self._close_icon = QIcon()
        self._available = 0
        self._from_width = float(MAX_TAB)
        self._to_width = float(MAX_TAB)
        self._growing: QWidget | None = None
        self._progress = Tween(self, 1.0, lambda _value: self._relayout())
        self._marker = Tween(self, 1.0, lambda _value: self.update())
        self._marker_from: QWidget | None = None
        self._current_button: QWidget | None = None
        self.currentChanged.connect(self._current_moved)
        self.apply_theme(theme)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        p = theme.palette
        self._pin_icon = glyph_icon("pin", p.text_secondary)
        self._close_icon = glyph_icon("close", p.text_muted, p.text)
        self.setFixedHeight(theme.metrics.tab_bar + 4)
        for index in range(self.count()):
            self._decorate(index)
        self.update()

    def set_pane_active(self, active: bool) -> None:
        if active != self._pane_active:
            self._pane_active = active
            self.update()

    def is_pinned(self, index: int) -> bool:
        return bool(self.tabData(index))

    def set_pinned(self, index: int, pinned: bool) -> None:
        self.setTabData(index, pinned)
        self._decorate(index)
        self._resize_tabs(None)

    def set_available(self, width: int) -> None:
        """The width the tabs may use. A resized window takes the new widths at once."""
        if width == self._available:
            return
        self._available = width
        self._growing = None
        self._from_width = self._to_width = float(self._target_width())
        self._progress.jump(1.0)
        self._relayout()

    def shared_width(self) -> float:
        """The width every unpinned tab has right now."""
        return self._from_width + (self._to_width - self._from_width) * float(self._progress.value)

    def _target_width(self) -> int:
        unpinned = [i for i in range(self.count()) if not self.is_pinned(i)]
        if not unpinned:
            return MAX_TAB
        pinned = sum(self._pinned_width(i) for i in range(self.count()) if self.is_pinned(i))
        room = self._available - pinned if self._available > 0 else MAX_TAB * len(unpinned)
        return max(MIN_TAB, min(MAX_TAB, room // len(unpinned)))

    def _pinned_width(self, index: int) -> int:
        name, _tone = split_mark(self.tabText(index))
        # The painted body is inset 2 px each side; inside it: 12 margin, 16 glyph, 8 gap, the name,
        # 12 margin, and 2 px so rounding never elides a name that fits.
        width = 2 + 12 + 16 + 8 + self.fontMetrics().horizontalAdvance(name) + 12 + 2 + 2
        return max(PINNED_MIN, min(MAX_TAB, width))

    def _resize_tabs(self, growing: QWidget | None) -> None:
        """Move from the widths on screen to the widths for the current tabs; ``growing`` (a new tab's close
        button) grows in at the same pace, so the tabs never need more room than they will end with."""
        self._from_width = self.shared_width()
        self._to_width = float(self._target_width())
        self._growing = growing
        self._progress.jump(0.0)
        self._progress.to(1.0, self._theme.ms(self._theme.motion.normal_ms), done=self._grown)

    def _grown(self) -> None:
        self._growing = None
        self._relayout()

    def _relayout(self) -> None:
        # Setting the icon size again marks Qt's tab layout dirty, so it asks for the tab sizes again.
        self.setIconSize(self.iconSize())

    def _decorate(self, index: int) -> None:
        self.setTabIcon(index, self._pin_icon if self.is_pinned(index) else QIcon())
        button = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
        if isinstance(button, QToolButton):
            button.setIcon(self._close_icon)
        self._sync_close_buttons()

    def _current_moved(self, index: int) -> None:
        """The marker slides from the tab that was current to the new one (the same tab moved by a drag
        keeps its marker)."""
        button = self.tabButton(index, QTabBar.ButtonPosition.RightSide) if index >= 0 else None
        previous, self._current_button = self._current_button, button
        if previous is None or button is None or previous is button or self._index_of(previous) < 0:
            self._marker.jump(1.0)
            return
        self._marker_from = previous
        self._marker.jump(0.0)
        self._marker.to(1.0, self._theme.ms(self._theme.motion.normal_ms))

    def _index_of(self, button: QWidget | None) -> int:
        for index in range(self.count()):
            if button is not None and self.tabButton(index, QTabBar.ButtonPosition.RightSide) is button:
                return index
        return -1

    def marker_rect(self, index: int) -> QRectF:
        body = QRectF(self.tabRect(index)).adjusted(2, 4, -2, 0)
        radius = float(self._theme.radius.lg)
        return QRectF(body.left() + radius, body.top(), body.width() - 2 * radius, 2)

    def sliding_marker(self) -> QRectF | None:
        """Where the marker is while it slides between tabs, or None when it sits on the current tab."""
        start, end = self._index_of(self._marker_from), self.currentIndex()
        if not self._marker.running() or start < 0 or end < 0 or self.property("home"):
            return None
        a, b = self.marker_rect(start), self.marker_rect(end)
        t = float(self._marker.value)
        return QRectF(
            a.left() + (b.left() - a.left()) * t, a.top(), a.width() + (b.width() - a.width()) * t, 2
        )

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        super().paintEvent(event)
        marker = self.sliding_marker()
        if marker is not None:
            painter = QPainter(self)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(
                QColor(self._theme.palette.accent if self._pane_active else self._theme.palette.marker)
            )
            painter.drawRoundedRect(marker, 1, 1)
            painter.end()

    def set_home(self, home: bool) -> None:
        """While Home is shown no tab is drawn as selected."""
        self.setProperty("home", home)
        self._sync_close_buttons()
        self.update()

    def _sync_close_buttons(self) -> None:
        current = -1 if self.property("home") else self.currentIndex()
        for index in range(self.count()):
            button = self.tabButton(index, QTabBar.ButtonPosition.RightSide)
            if isinstance(button, QToolButton):
                show = not self.is_pinned(index) and index in (current, self._hover)
                button.setVisible(show)

    def tabSizeHint(self, index: int) -> QSize:  # noqa: N802 - Qt override
        height = self._theme.metrics.tab_bar + 4
        if self.is_pinned(index):
            return QSize(self._pinned_width(index), height)
        width = self.shared_width()
        growing = self._growing
        if growing is not None and self.tabButton(index, QTabBar.ButtonPosition.RightSide) is growing:
            width = self._to_width * float(self._progress.value)
        return QSize(max(0, round(width)), height)

    def minimumTabSizeHint(self, index: int) -> QSize:  # noqa: N802 - Qt override
        """The same as the size hint: Qt never squeezes some tabs more than others."""
        return self.tabSizeHint(index)

    def paint_scroll_arrow(self, painter: QPainter, rect: QRectF, left: bool) -> None:
        """The arrows that scroll tabs into view when they do not all fit, drawn like the other glyphs."""
        side = 14.0
        box = QRectF(rect.center().x() - side / 2, rect.center().y() - side / 2, side, side)
        draw_glyph(painter, "back" if left else "forward", box, QColor(self._theme.palette.text_secondary))

    def paint_tab(self, painter: QPainter, option: QStyleOptionTab) -> None:
        """Called by the application style for every tab, including one being dragged."""
        t = self._theme
        p = t.palette
        rect = QRectF(option.rect)  # type: ignore[attr-defined]
        state = option.state  # type: ignore[attr-defined]
        selected = bool(state & QStyle.StateFlag.State_Selected) and not self.property("home")
        hovered = bool(state & QStyle.StateFlag.State_MouseOver)
        pinned = not option.icon.isNull()  # type: ignore[attr-defined]
        name, tone = split_mark(option.text)  # type: ignore[attr-defined]
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        body = rect.adjusted(2, 4, -2, 0)
        radius = float(t.radius.lg)
        if selected:
            path = QPainterPath()
            path.moveTo(body.left(), body.bottom() + 1)
            path.lineTo(body.left(), body.top() + radius)
            path.quadTo(body.left(), body.top(), body.left() + radius, body.top())
            path.lineTo(body.right() - radius, body.top())
            path.quadTo(body.right(), body.top(), body.right(), body.top() + radius)
            path.lineTo(body.right(), body.bottom() + 1)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.page))
            painter.drawPath(path)
            if self.sliding_marker() is None:
                marker = QRectF(body.left() + radius, body.top(), body.width() - 2 * radius, 2)
                painter.setBrush(QColor(p.accent if self._pane_active else p.marker))
                painter.drawRoundedRect(marker, 1, 1)
        elif hovered:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.hover))
            painter.drawRoundedRect(body.adjusted(0, 2, 0, -4), t.radius.md, t.radius.md)
        next_selected = option.selectedPosition == QStyleOptionTab.SelectedPosition.NextIsSelected  # type: ignore[attr-defined]
        if not selected and not hovered and not (next_selected and not self.property("home")):
            x = rect.right() - 0.5
            centre = body.center().y() - 1
            painter.setPen(QPen(QColor(p.border), 1))
            painter.drawLine(QPointF(x, centre - 8), QPointF(x, centre + 8))
        mid = body.center().y() + (1 if selected else -1)
        left = body.left() + 12
        closable = not pinned and (selected or hovered)
        if closable and body.width() < 16 + 12 + CLOSE_ROOM + 12:
            painter.restore()
            return
        if tone:
            paint_dot(painter, QPointF(left + 8, mid), tone, p, 3.5)
        else:
            glyph = "pin" if pinned else "note"
            draw_glyph(
                painter,
                glyph,
                QRectF(left, mid - 8, 16, 16),
                QColor(p.text_secondary if selected else p.text_muted),
            )
        text_left = left + 16 + 8
        right = body.right() - (CLOSE_ROOM + 2 if closable else 12)
        width = max(0.0, right - text_left)
        text = self.fontMetrics().elidedText(name, Qt.TextElideMode.ElideRight, int(width))
        if text.strip("\N{HORIZONTAL ELLIPSIS}"):
            painter.setFont(self.font())
            painter.setPen(QColor(p.text if selected else p.text_secondary))
            painter.drawText(
                QRectF(text_left, body.top(), width, body.height() - (0 if selected else 2)),
                int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
                text,
            )
        painter.restore()

    def tabInserted(self, index: int) -> None:  # noqa: N802 - Qt override
        super().tabInserted(index)
        close = QToolButton(self)
        close.setObjectName("TabClose")
        close.setAutoRaise(True)
        close.setToolTip("Close tab (Ctrl+W)")
        close.setAccessibleName("Close tab")
        close.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close.setFixedSize(20, 20)
        close.clicked.connect(lambda _c=False, b=close: self._close_clicked(b))
        self.setTabButton(index, QTabBar.ButtonPosition.RightSide, close)
        self._decorate(index)
        self._resize_tabs(close)

    def tabRemoved(self, index: int) -> None:  # noqa: N802 - Qt override
        super().tabRemoved(index)
        self._hover = -1
        self._sync_close_buttons()
        self._resize_tabs(None)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        super().mouseMoveEvent(event)
        hover = self.tabAt(event.position().toPoint())
        if hover != self._hover:
            self._hover = hover
            self._sync_close_buttons()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.Leave and self._hover != -1:
            self._hover = -1
            self._sync_close_buttons()
        return super().event(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.MiddleButton:
            index = self.tabAt(event.position().toPoint())
            if index >= 0:
                self.close_requested.emit(index)
                return
        super().mouseReleaseEvent(event)

    def _close_clicked(self, button: QToolButton) -> None:
        for index in range(self.count()):
            if self.tabButton(index, QTabBar.ButtonPosition.RightSide) is button:
                self.close_requested.emit(index)
                return

    def menu_for(self, index: int) -> QMenu:
        menu = QMenu(self)

        def add(text: str, handler: Callable[[], None], shortcut: str = "") -> QAction:
            action = menu.addAction(text)
            if shortcut:
                action.setShortcut(QKeySequence(shortcut))
            action.triggered.connect(handler)
            return action

        pinned = self.is_pinned(index)
        add("Unpin tab" if pinned else "Pin tab", lambda: self.pin_toggled.emit(index))
        add("Version history", lambda: self.history_requested.emit(index), "Ctrl+Shift+H")
        add("Copy path", lambda: self.copy_path_requested.emit(index), "Ctrl+Shift+C")
        menu.addSeparator()
        add("Close tab", lambda: self.close_requested.emit(index), "Ctrl+W")
        add("Close other tabs", lambda: self.close_others_requested.emit(index))
        add("Reopen closed tab", self.reopen_requested.emit, "Ctrl+Shift+T")
        return menu

    def _context_menu(self, pos: QPoint) -> None:
        index = self.tabAt(pos)
        if index >= 0:
            self.menu_for(index).popup(self.mapToGlobal(pos))


class TabOverflow(QToolButton):
    """Lists every open tab, so a tab scrolled out of view is always one click away."""

    activate_requested = Signal(int)

    def __init__(self, strip: TabStrip, theme: Theme) -> None:
        super().__init__()
        self._strip = strip
        self.setAccessibleName("All open tabs")
        self.setToolTip("All open tabs")
        self.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.setObjectName("StripButton")
        self._menu = QMenu(self)
        self._menu.aboutToShow.connect(self._fill)
        self.setMenu(self._menu)
        self.apply_theme(theme)

    def apply_theme(self, theme: Theme) -> None:
        p = theme.palette
        self.setIcon(glyph_icon("chevron_down", p.text_secondary, p.text))

    def _fill(self) -> None:
        self._menu.clear()
        for index in range(self._strip.count()):
            name, _tone = split_mark(self._strip.tabText(index))
            action = self._menu.addAction(name)
            action.setCheckable(True)
            action.setChecked(index == self._strip.currentIndex())
            action.triggered.connect(lambda _c=False, i=index: self.activate_requested.emit(i))
