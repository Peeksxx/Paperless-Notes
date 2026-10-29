"""A layout that places widgets left to right and wraps to a new row when the width runs out, so button rows
never clip their labels in a narrow window or at a large display scale."""

from __future__ import annotations

from typing import cast

from PySide6.QtCore import QMargins, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import QLayout, QLayoutItem, QWidget, QWidgetItem


class FlowLayout(QLayout):
    def __init__(self, parent: QWidget | None = None, spacing: int = 6) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self._gap = spacing
        self.setContentsMargins(QMargins(0, 0, 0, 0))

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def insert_widget(self, index: int, widget: QWidget) -> None:
        """Place ``widget`` at ``index``, or at the end when ``index`` is out of range."""
        self.addChildWidget(widget)
        at = index if 0 <= index <= len(self._items) else len(self._items)
        self._items.insert(at, QWidgetItem(widget))
        self.invalidate()

    def _shown(self) -> list[QLayoutItem]:
        return [i for i in self._items if (w := i.widget()) is None or not w.isHidden()]

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem:
        """Qt's contract returns null past the end; the stub omits it, but layout teardown relies on it."""
        return self._items.pop(index) if 0 <= index < len(self._items) else cast(QLayoutItem, None)

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self) -> QSize:
        shown = self._shown()
        width = sum(i.sizeHint().width() for i in shown) + self._gap * max(0, len(shown) - 1)
        height = max((i.sizeHint().height() for i in shown), default=0)
        m = self.contentsMargins()
        return QSize(width + m.left() + m.right(), height + m.top() + m.bottom())

    def minimumSize(self) -> QSize:
        size = QSize()
        for item in self._shown():
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _arrange(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, row_height = area.x(), area.y(), 0
        for item in self._shown():
            hint = item.sizeHint()
            if x > area.x() and x + hint.width() > area.right() + 1:
                x = area.x()
                y += row_height + self._gap
                row_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), QSize(min(hint.width(), area.width()), hint.height())))
            x += hint.width() + self._gap
            row_height = max(row_height, hint.height())
        return y + row_height - rect.y() + m.bottom()
