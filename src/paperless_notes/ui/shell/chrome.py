"""Window chrome: one app bar that is also the title bar, native move and resize, snap, and DWM decoration.

The window uses Qt's frameless hint and the platform's own move and resize loops (``startSystemMove`` and
``startSystemResize``), so dragging to a screen edge snaps and maximizing respects the work area. Rounded
corners, the shadow, the border colour and dark mode are requested from DWM. The "Use native window frame"
setting replaces the caption buttons and dragging with the system frame on the next start.
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from PySide6.QtCore import QEvent, QObject, QPoint, QSize, Qt, Signal
from PySide6.QtGui import QMouseEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import QApplication, QFrame, QHBoxLayout, QMenu, QSizePolicy, QWidget

from paperless_notes.ui.shell.topbar import Breadcrumbs, SearchBox, crumbs_for, icon_button
from paperless_notes.ui.shell.widgets import CaptionButton
from paperless_notes.ui.theme.icons import glyph_icon
from paperless_notes.ui.theme.tokens import Theme

logger = logging.getLogger(__name__)

RESIZE_BORDER = 6
_CURSORS = {
    Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
    Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
    Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
}
_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_BORDER_COLOR = 34
_ROUND = 2
_ROUND_SMALL = 3
_NO_ROUND = 1


def edges_at(pos: QPoint, size: QSize, border: int = RESIZE_BORDER) -> Qt.Edge:
    """The window edges under ``pos`` (window coordinates), for resizing a frameless window."""
    edges = Qt.Edge(0)
    if pos.x() < border:
        edges |= Qt.Edge.LeftEdge
    elif pos.x() >= size.width() - border:
        edges |= Qt.Edge.RightEdge
    if pos.y() < border:
        edges |= Qt.Edge.TopEdge
    elif pos.y() >= size.height() - border:
        edges |= Qt.Edge.BottomEdge
    return edges


def cursor_for(edges: Qt.Edge) -> Qt.CursorShape | None:
    if not edges:
        return None
    left_or_right = edges & (Qt.Edge.LeftEdge | Qt.Edge.RightEdge)
    top_or_bottom = edges & (Qt.Edge.TopEdge | Qt.Edge.BottomEdge)
    if left_or_right and top_or_bottom:
        falling = edges in (Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
        return Qt.CursorShape.SizeFDiagCursor if falling else Qt.CursorShape.SizeBDiagCursor
    for edge, shape in _CURSORS.items():
        if edges & edge:
            return shape
    return None


class _Margins(ctypes.Structure):
    _fields_ = (
        ("left", ctypes.c_int),
        ("right", ctypes.c_int),
        ("top", ctypes.c_int),
        ("bottom", ctypes.c_int),
    )


def colorref(color: str) -> int:
    """``#rrggbb`` as a Win32 COLORREF (0x00bbggrr)."""
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return r | (g << 8) | (b << 16)


def _set_attribute(dwm: ctypes.WinDLL, hwnd: wintypes.HWND, attribute: int, value: int) -> None:
    data = ctypes.c_int(value)
    dwm.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(data), ctypes.sizeof(data))


def request_native_decoration(window: QWidget, maximized: bool, theme: Theme | None = None) -> bool:
    """Ask DWM for the shadow, rounded corners when not maximized, dark mode and a border in the theme's
    hairline colour. Cosmetic: failures are logged."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        hwnd = wintypes.HWND(int(window.winId()))
        dwm.DwmExtendFrameIntoClientArea(hwnd, ctypes.byref(_Margins(1, 1, 1, 1)))
        _set_attribute(dwm, hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, _NO_ROUND if maximized else _ROUND)
        if theme is not None:
            _set_attribute(dwm, hwnd, _DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if theme.dark else 0)
            _set_attribute(dwm, hwnd, _DWMWA_BORDER_COLOR, colorref(theme.palette.border))
    except OSError as exc:
        logger.info("Native window decoration unavailable: %s", exc)
        return False
    return True


def round_popup(widget: QWidget, theme: Theme) -> bool:
    """Rounded corners and a border colour for a menu or tooltip window on Windows 11. Cosmetic."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        hwnd = wintypes.HWND(int(widget.winId()))
        _set_attribute(dwm, hwnd, _DWMWA_WINDOW_CORNER_PREFERENCE, _ROUND_SMALL)
        _set_attribute(dwm, hwnd, _DWMWA_BORDER_COLOR, colorref(theme.palette.border_strong))
    except OSError as exc:
        logger.debug("Popup decoration unavailable: %s", exc)
        return False
    return True


class PopupRounder(QObject):
    """Rounds every menu the application shows."""

    def __init__(self, theme: Theme, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.theme = theme
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if event.type() == QEvent.Type.Show and isinstance(watched, QMenu):
            round_popup(watched, self.theme)
        return False


class AppBar(QWidget):
    """Home, sidebar, back and forward, breadcrumbs, the search box (centred on the window), history, help,
    settings and the caption buttons. Empty space drags the window; a double click maximizes."""

    home_requested = Signal()
    sidebar_toggled = Signal()
    back_requested = Signal()
    forward_requested = Signal()
    search_requested = Signal()
    history_requested = Signal()
    help_requested = Signal()
    settings_requested = Signal()
    minimize_requested = Signal()
    maximize_toggled = Signal()
    close_requested = Signal()

    def __init__(self, window: QWidget, theme: Theme, native_frame: bool = False) -> None:
        super().__init__(window)
        self.setObjectName("AppBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._window = window
        self._native = native_frame
        self._theme = theme
        self.setFixedHeight(theme.metrics.title_bar)
        s = theme.spacing
        layout = QHBoxLayout(self)
        layout.setContentsMargins(s.sm, 0, 0, 0)
        layout.setSpacing(0)
        self.left = QWidget()
        left = QHBoxLayout(self.left)
        left.setContentsMargins(0, 0, 0, 0)
        left.setSpacing(s.xxs)
        self.home_button = icon_button("Home", "Home: recent notes, changes and tags (Alt+Home)")
        self.sidebar_button = icon_button("Sidebar", "Show or hide the sidebar (Ctrl+\\)")
        self.back_button = icon_button("Back", "Back to the previous note (Alt+Left)")
        self.forward_button = icon_button("Forward", "Forward (Alt+Right)")
        for button, signal in (
            (self.home_button, self.home_requested),
            (self.sidebar_button, self.sidebar_toggled),
            (self.back_button, self.back_requested),
            (self.forward_button, self.forward_requested),
        ):
            button.clicked.connect(signal.emit)
            left.addWidget(button)
        left.addSpacing(s.sm)
        self.crumbs = Breadcrumbs()
        left.addWidget(self.crumbs, 1)
        left.addStretch(0)
        self.left.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.left, 1)
        layout.addStretch(0)
        self.right = QWidget()
        self.right.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Preferred)
        right = QHBoxLayout(self.right)
        right.setContentsMargins(s.md, 0, 0, 0)
        right.setSpacing(s.xxs)
        self.history_button = icon_button("Version history", "Version history of this note (Ctrl+Shift+H)")
        self.help_button = icon_button("Help", "Help and keyboard shortcuts (F1)")
        self.settings_button = icon_button("Settings", "Settings (Ctrl+,)")
        for button, signal in (
            (self.history_button, self.history_requested),
            (self.help_button, self.help_requested),
            (self.settings_button, self.settings_requested),
        ):
            button.clicked.connect(signal.emit)
            right.addWidget(button)
        right.addSpacing(s.md)
        self.divider = QFrame()
        self.divider.setFrameShape(QFrame.Shape.VLine)
        self.divider.setFixedSize(1, 18)
        self.divider.setObjectName("BarDivider")
        right.addWidget(self.divider)
        right.addSpacing(s.xs)
        self.minimize_button = CaptionButton("minimize", theme)
        self.maximize_button = CaptionButton("maximize", theme)
        self.close_button = CaptionButton("close", theme)
        for caption, name, tip, signal in (
            (self.minimize_button, "Minimize", "Minimize", self.minimize_requested),
            (self.maximize_button, "Maximize", "Maximize (Win+Up)", self.maximize_toggled),
            (self.close_button, "Close", "Close Paperless Notes (Alt+F4)", self.close_requested),
        ):
            caption.setAccessibleName(name)
            caption.setToolTip(tip)
            caption.clicked.connect(signal.emit)
            caption.setVisible(not native_frame)
            right.addWidget(caption)
        self.divider.setVisible(not native_frame)
        if native_frame:
            right.addSpacing(s.sm)
        layout.addWidget(self.right)
        self.search = SearchBox(theme, self)
        self.search.clicked.connect(self.search_requested.emit)
        self.apply_theme(theme)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        p = theme.palette
        for button, name in (
            (self.home_button, "logo"),
            (self.sidebar_button, "sidebar"),
            (self.back_button, "back"),
            (self.forward_button, "forward"),
            (self.history_button, "history"),
            (self.help_button, "help"),
            (self.settings_button, "settings"),
        ):
            button.setIcon(glyph_icon(name, p.text_secondary, p.text, p.marker))
        for caption in (self.minimize_button, self.maximize_button, self.close_button):
            caption.apply_theme(theme)
        self.search.apply_theme(theme)

    def set_maximized(self, maximized: bool, _theme: Theme | None = None) -> None:
        self.maximize_button.set_kind("restore" if maximized else "maximize")
        self.maximize_button.setAccessibleName("Restore" if maximized else "Maximize")
        self.maximize_button.setToolTip("Restore down (Win+Down)" if maximized else "Maximize (Win+Up)")

    def set_window_active(self, active: bool) -> None:
        for caption in (self.minimize_button, self.maximize_button, self.close_button):
            caption.set_window_active(active)

    def set_navigation(self, can_back: bool, can_forward: bool) -> None:
        self.back_button.setEnabled(can_back)
        self.forward_button.setEnabled(can_forward)

    def set_note(self, path: str | None, roots: list[str]) -> None:
        self.crumbs.set_crumbs(crumbs_for(path, roots) if path else [])
        self.crumbs.setVisible(path is not None)
        self.history_button.setEnabled(path is not None)

    def _place_search(self) -> None:
        width = self.width()
        m = self._theme.metrics
        right_edge = width - self.right.sizeHint().width() - 12
        fixed_left = self.layout().contentsMargins().left() + sum(  # type: ignore[union-attr]
            b.sizeHint().width() + 2
            for b in (self.home_button, self.sidebar_button, self.back_button, self.forward_button)
        )
        box = max(160, min(560, round(width * 0.34)))
        x = (width - box) // 2
        if x + box > right_edge:
            x = max(fixed_left + 12, right_edge - box)
            box = max(120, right_edge - x)
        self.search.setGeometry(x, (self.height() - m.search_box) // 2, box, m.search_box)
        self.search.setVisible(right_edge - (fixed_left + 12) >= 120)
        self.left.setMaximumWidth(max(fixed_left, x - 16 - self.left.x()))
        self.search.raise_()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place_search()

    def showEvent(self, event: QShowEvent) -> None:  # noqa: N802 - Qt override
        super().showEvent(event)
        self._place_search()

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and not self._native:
            handle = self._window.windowHandle()
            if handle is not None and handle.startSystemMove():
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton and not self._native:
            self.maximize_toggled.emit()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class EdgeResizer(QObject):
    """Resizes a frameless window from a thin band along its edges using the native resize loop."""

    def __init__(self, window: QWidget, border: int = RESIZE_BORDER) -> None:
        super().__init__(window)
        self._window = window
        self._border = border
        self._cursor_set = False
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)

    def _edges(self, event: QMouseEvent) -> Qt.Edge:
        if self._window.isMaximized() or self._window.isFullScreen():
            return Qt.Edge(0)
        local = self._window.mapFromGlobal(event.globalPosition().toPoint())
        if not self._window.rect().contains(local):
            return Qt.Edge(0)
        return edges_at(local, self._window.size(), self._border)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        kind = event.type()
        if kind not in (QEvent.Type.MouseMove, QEvent.Type.MouseButtonPress) or not isinstance(
            event, QMouseEvent
        ):
            return False
        if not isinstance(watched, QWidget) or watched.window() is not self._window:
            return False
        edges = self._edges(event)
        shape = cursor_for(edges)
        if kind == QEvent.Type.MouseMove and not event.buttons():
            self._set_cursor(shape)
            return False
        if kind == QEvent.Type.MouseButtonPress and event.button() == Qt.MouseButton.LeftButton and edges:
            handle = self._window.windowHandle()
            if handle is not None and handle.startSystemResize(edges):
                return True
        return False

    def _set_cursor(self, shape: Qt.CursorShape | None) -> None:
        if shape is None and self._cursor_set:
            QApplication.restoreOverrideCursor()
            self._cursor_set = False
        elif shape is not None:
            if self._cursor_set:
                QApplication.changeOverrideCursor(shape)
            else:
                QApplication.setOverrideCursor(shape)
                self._cursor_set = True
