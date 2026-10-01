"""Short covers for switching between Home and a note: a picture of the view laid over the real one, which
ignores the mouse and removes itself when its animation ends, so the real view is usable at once."""

from __future__ import annotations

from collections.abc import Callable
from itertools import pairwise

from PySide6.QtCore import QEasingCurve, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPixmap
from PySide6.QtWidgets import QWidget

from paperless_notes.ui.shell.motion import animate

FADE_MS = 140
STAGGER_MS = 60
SECTION_MS = 220
RISE = 10


def ease_out(value: float) -> float:
    return QEasingCurve(QEasingCurve.Type.OutCubic).valueForProgress(max(0.0, min(1.0, value)))


class Cover(QWidget):
    """Paints ``paint(painter, elapsed_ms)`` over ``host`` for ``duration`` ms, then deletes itself."""

    def __init__(self, host: QWidget, paint: Callable[[QPainter, float], None], duration: int) -> None:
        super().__init__(host)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setGeometry(host.rect())
        self._paint = paint
        self._duration = duration
        self.elapsed = 0.0
        self.show()
        self.raise_()
        animate(self, 0.0, 1.0, duration, self._step, self.finish, QEasingCurve.Type.Linear)

    def _step(self, value: float) -> None:
        self.elapsed = float(value) * self._duration
        self.update()

    def finish(self) -> None:
        self.hide()
        self.deleteLater()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        self._paint(painter, self.elapsed)
        painter.end()


def fade_out(host: QWidget, before: QPixmap, duration: int) -> Cover | None:
    """The previous view fades away over the new one."""
    if duration <= 0 or before.isNull():
        return None

    def paint(painter: QPainter, elapsed: float) -> None:
        painter.setOpacity(1.0 - ease_out(elapsed / duration))
        painter.drawPixmap(0, 0, before)

    return Cover(host, paint, duration)


def rise_in(host: QWidget, before: QPixmap | None, after: QPixmap, tops: list[int]) -> Cover | None:
    """Home appearing: the previous view fades while Home's sections (split at ``tops``) rise into place
    one after another."""
    if after.isNull():
        return None
    image = after.toImage()
    background = QColor(image.pixelColor(1, 1)) if not image.isNull() else QColor(Qt.GlobalColor.white)
    height = host.height()
    edges = [*sorted({max(0, min(height, top)) for top in tops} | {0}), height]
    bands = [QRect(0, a, host.width(), b - a) for a, b in pairwise(edges) if b > a]
    duration = STAGGER_MS * max(0, len(bands) - 1) + SECTION_MS
    ratio = after.devicePixelRatio()

    def paint(painter: QPainter, elapsed: float) -> None:
        painter.fillRect(host.rect(), background)
        if before is not None and not before.isNull():
            painter.setOpacity(1.0 - ease_out(elapsed / FADE_MS))
            painter.drawPixmap(0, 0, before)
        for index, band in enumerate(bands):
            shown = ease_out((elapsed - index * STAGGER_MS) / SECTION_MS)
            if shown <= 0:
                continue
            painter.setOpacity(shown)
            source = QRectF(band.x() * ratio, band.y() * ratio, band.width() * ratio, band.height() * ratio)
            target = QRectF(band).translated(0, (1.0 - shown) * RISE)
            painter.drawPixmap(target, after, source)

    return Cover(host, paint, duration)
