"""Icons painted in memory (SEC5): nothing is written to or read from %TEMP%."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap


def paint_cross(size: int, color: str, device_ratio: float = 1.0) -> QPixmap:
    pixels = max(1, round(size * device_ratio))
    pixmap = QPixmap(pixels, pixels)
    pixmap.setDevicePixelRatio(device_ratio)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    try:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(QColor(color))
        pen.setWidthF(max(1.5, size / 8))
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        margin = size * 0.25
        painter.drawLine(QPointF(margin, margin), QPointF(size - margin, size - margin))
        painter.drawLine(QPointF(size - margin, margin), QPointF(margin, size - margin))
    finally:
        painter.end()
    return pixmap


def close_icon(color: str = "#94a3b8", hover_color: str = "#e2e8f0", size: int = 14) -> QIcon:
    icon = QIcon()
    for ratio in (1.0, 1.25, 1.5, 2.0):
        icon.addPixmap(paint_cross(size, color, ratio), QIcon.Mode.Normal)
        icon.addPixmap(paint_cross(size, hover_color, ratio), QIcon.Mode.Active)
    return icon
