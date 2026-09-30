"""The application style: Fusion, plus the library tree's chevrons and indentation guides and the tab strip's
tabs. Tabs are painted here so a tab being dragged is drawn the same way as the others."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainter, QPalette, QPen
from PySide6.QtWidgets import QProxyStyle, QStyle, QStyleOption, QStyleOptionTab, QWidget

from paperless_notes.ui.theme.icons import draw_glyph

TREE = "LibraryTree"
TABS = "TabStrip"


def _named(widget: QWidget | None, name: str) -> bool:
    return widget is not None and widget.objectName() == name


class ShellStyle(QProxyStyle):
    def __init__(self) -> None:
        super().__init__("Fusion")

    def drawPrimitive(  # noqa: N802 - Qt override
        self,
        element: QStyle.PrimitiveElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        if _named(widget, TREE):
            if element == QStyle.PrimitiveElement.PE_IndicatorBranch:
                self._branch(option, painter)
                return
            if element in (
                QStyle.PrimitiveElement.PE_PanelItemViewRow,
                QStyle.PrimitiveElement.PE_PanelItemViewItem,
                QStyle.PrimitiveElement.PE_FrameFocusRect,
            ):
                return
        if element == QStyle.PrimitiveElement.PE_FrameFocusRect and widget is not None:
            return
        super().drawPrimitive(element, option, painter, widget)

    def drawControl(  # noqa: N802 - Qt override
        self,
        element: QStyle.ControlElement,
        option: QStyleOption,
        painter: QPainter,
        widget: QWidget | None = None,
    ) -> None:
        hook = getattr(widget, "paint_tab", None) if _named(widget, TABS) else None
        if (
            element == QStyle.ControlElement.CE_TabBarTab
            and callable(hook)
            and isinstance(option, QStyleOptionTab)
        ):
            hook(painter, option)
            return
        if element in (
            QStyle.ControlElement.CE_TabBarTabShape,
            QStyle.ControlElement.CE_TabBarTabLabel,
        ) and callable(hook):
            return
        super().drawControl(element, option, painter, widget)

    @staticmethod
    def _branch(option: QStyleOption, painter: QPainter) -> None:
        rect = QRectF(option.rect)  # type: ignore[attr-defined]
        state = option.state  # type: ignore[attr-defined]
        palette: QPalette = option.palette  # type: ignore[attr-defined]
        item = bool(state & QStyle.StateFlag.State_Item)
        painter.save()
        if not item:
            painter.setPen(QPen(palette.color(QPalette.ColorRole.Mid), 1))
            x = round(rect.center().x()) + 0.5
            painter.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom() + 1))
        elif state & QStyle.StateFlag.State_Children:
            name = "chevron_down" if state & QStyle.StateFlag.State_Open else "chevron_right"
            color = QColor(palette.color(QPalette.ColorRole.PlaceholderText))
            side = 12.0
            draw_glyph(
                painter,
                name,
                QRectF(rect.center().x() - side / 2, rect.center().y() - side / 2, side, side),
                color,
                1.7,
            )
        painter.restore()
