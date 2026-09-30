"""Pieces of the app bar: breadcrumbs with overflow, and the search box that opens the search palette."""

from __future__ import annotations

import ntpath

from PySide6.QtCore import QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QEnterEvent, QFontMetrics, QPainter, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import QAbstractButton, QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton, QWidget

from paperless_notes.core import pathid
from paperless_notes.ui.shell.widgets import keycaps_width, meta_font, paint_keycaps
from paperless_notes.ui.theme.icons import draw_glyph
from paperless_notes.ui.theme.tokens import Theme

SEARCH_SHORTCUT = "Ctrl+Shift+P"
SEARCH_TIP = (
    "Search note names and text (Ctrl+Shift+P). Type > for commands, # for tags, @ for headings in this "
    "note, : for a line number, ? for help."
)


def crumbs_for(path: str, roots: list[str]) -> list[tuple[str, str]]:
    """(label, path) from the library root that holds ``path`` (or the drive) down to the note."""
    normalized = pathid.normalize(path)
    root = next(
        (r for r in roots if pathid.is_within(normalized, r) or pathid.same_path(normalized, r)), None
    )
    parts: list[tuple[str, str]] = []
    current = normalized
    while True:
        parent, name = ntpath.split(current)
        if not name:
            if current:
                parts.append((current.rstrip("\\") or current, current))
            break
        parts.append((name, current))
        if root is not None and pathid.same_path(current, root):
            break
        if parent == current:
            break
        current = parent
    return list(reversed(parts))


def icon_button(name: str, tooltip: str) -> QToolButton:
    button = QToolButton()
    button.setAccessibleName(name)
    button.setToolTip(tooltip)
    button.setAutoRaise(True)
    return button


class Breadcrumbs(QWidget):
    """Folder path of the current note. Leading folders collapse into a menu when space runs out."""

    folder_requested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setAccessibleName("Location of this note")
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(0)
        self.overflow = QToolButton()
        self.overflow.setText("...")
        self.overflow.setAccessibleName("Hidden folders in the path")
        self.overflow.setToolTip("Show the rest of the path")
        self.overflow.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.overflow.setMenu(QMenu(self.overflow))
        self.overflow.setObjectName("CrumbOverflow")
        self.overflow.setProperty("kind", "labelled")
        self.overflow.setMinimumWidth(self.overflow.sizeHint().width())
        self.setMinimumWidth(0)
        self._layout.addWidget(self.overflow)
        self.buttons: list[QToolButton] = []
        self.current = QLabel("")
        self.current.setProperty("role", "crumb-current")
        self.current.setMinimumWidth(0)
        self.current.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self._layout.addWidget(self.current, 1)
        self._crumbs: list[tuple[str, str]] = []

    def set_crumbs(self, crumbs: list[tuple[str, str]]) -> None:
        """Folders are buttons that reveal the folder; the note itself is plain, elided text."""
        for button in self.buttons:
            self._layout.removeWidget(button)
            button.deleteLater()
        self.buttons = []
        self._crumbs = crumbs
        for index, (label, path) in enumerate(crumbs[:-1]):
            button = QToolButton()
            button.setText(f"{label}  \N{SINGLE RIGHT-POINTING ANGLE QUOTATION MARK}")
            button.setProperty("kind", "labelled")
            button.setToolTip(f"Show {path} in the sidebar")
            button.setAccessibleName(label)
            button.clicked.connect(lambda _c=False, p=path: self.folder_requested.emit(p))
            self._layout.insertWidget(1 + index, button)
            self.buttons.append(button)
        name, path = crumbs[-1] if crumbs else ("", "")
        self.current.setToolTip(path)
        self.current.setAccessibleName(name)
        self._fit()

    def visible_labels(self) -> list[str]:
        folders = [
            label for (label, _), b in zip(self._crumbs, self.buttons, strict=False) if not b.isHidden()
        ]
        return [*folders, self._crumbs[-1][0]] if self._crumbs else []

    def _fit(self) -> None:
        name = self._crumbs[-1][0] if self._crumbs else ""
        metrics = self.current.fontMetrics()
        need = metrics.horizontalAdvance(name) + 8
        available = self.width()
        widths = [b.sizeHint().width() for b in self.buttons]
        overflow = self.overflow.sizeHint().width()
        minimum = min(need, 120)
        hidden = 0
        while (
            hidden < len(widths) and sum(widths[hidden:]) + (overflow if hidden else 0) + minimum > available
        ):
            hidden += 1
        show_overflow = hidden > 0 and overflow + minimum <= available
        menu = self.overflow.menu()
        menu.clear()
        for index, button in enumerate(self.buttons):
            button.setHidden(index < hidden)
            if index < hidden:
                label, path = self._crumbs[index]
                action = menu.addAction(label)
                action.triggered.connect(lambda _c=False, p=path: self.folder_requested.emit(p))
        self.overflow.setVisible(show_overflow)
        room = available - sum(widths[hidden:]) - (overflow if show_overflow else 0) - 8
        text = metrics.elidedText(name, Qt.TextElideMode.ElideMiddle, max(0, room))
        fits = metrics.horizontalAdvance(text) <= room and text.strip("\N{HORIZONTAL ELLIPSIS}")
        self.current.setText(text if fits else "")

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit()


class SearchBox(QAbstractButton):
    """Looks like a field, opens the search palette. Says what it searches and how to reach commands."""

    def __init__(self, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("SearchBox")
        self.setAccessibleName("Search")
        self.setToolTip(SEARCH_TIP)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._theme = theme
        self._hover = False

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(420, self._theme.metrics.search_box)

    def enterEvent(self, event: QEnterEvent) -> None:  # noqa: N802 - Qt override
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        self._hover = False
        self.update()
        super().leaveEvent(event)  # type: ignore[arg-type]

    def label(self) -> str:
        """The visible prompt for the current width."""
        font_metrics = self.fontMetrics()
        room = self.width() - 44 - keycaps_width(SEARCH_SHORTCUT, meta_font(self._theme))
        for text in ("Search notes, or type > for commands", "Search notes and commands", "Search"):
            if font_metrics.horizontalAdvance(text) <= room:
                return text
        return "Search"

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        t = self._theme
        p = t.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        active = self._hover or self.isDown()
        painter.setPen(QPen(QColor(p.border_strong if active else p.border), 1))
        painter.setBrush(QColor(p.hover if self.isDown() else p.surface))
        painter.drawRoundedRect(rect, t.radius.md, t.radius.md)
        if self.hasFocus():
            painter.setPen(QPen(QColor(p.focus), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(rect.adjusted(1, 1, -1, -1), t.radius.md, t.radius.md)
        mid = self.height() / 2
        draw_glyph(
            painter,
            "search",
            QRectF(10, mid - 7, 14, 14),
            QColor(p.text_secondary if active else p.text_muted),
        )
        font = meta_font(t)
        room = self.width() - 34
        caps = keycaps_width(SEARCH_SHORTCUT, font)
        right = self.width() - 6.0
        if room - caps > QFontMetrics(self.font()).horizontalAdvance("Search") + 20:
            right -= paint_keycaps(painter, right, mid, SEARCH_SHORTCUT, font, p) + 8
        painter.setFont(self.font())
        painter.setPen(QColor(p.text_secondary if active else p.text_muted))
        painter.drawText(
            QRectF(32, 0, max(0.0, right - 32), self.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self.label(),
        )
