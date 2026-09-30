"""One monochrome icon language, painted as vectors on a 16 by 16 grid at any size and pixel ratio.

Stroke 1.5 units, round caps and joins, no fills except small dots. Nothing is read from disk (SEC5) and no
SVG module is loaded.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPainterPath, QPen, QPixmap

type Glyph = Callable[[QPainter], None]


def _path(*points: tuple[float, float], close: bool = False) -> QPainterPath:
    path = QPainterPath(QPointF(*points[0]))
    for point in points[1:]:
        path.lineTo(QPointF(*point))
    if close:
        path.closeSubpath()
    return path


def _lines(*segments: tuple[float, float, float, float]) -> Glyph:
    def draw(p: QPainter) -> None:
        for x1, y1, x2, y2 in segments:
            p.drawLine(QPointF(x1, y1), QPointF(x2, y2))

    return draw


def _paths(*paths: QPainterPath, extra: Glyph | None = None) -> Glyph:
    def draw(p: QPainter) -> None:
        for path in paths:
            p.drawPath(path)
        if extra is not None:
            extra(p)

    return draw


def _note(p: QPainter) -> None:
    p.drawPath(_path((4, 2), (10, 2), (13, 5), (13, 14), (4, 14), close=True))
    p.drawPath(_path((10, 2), (10, 5), (13, 5)))


def _folder(p: QPainter) -> None:
    p.drawPath(_path((2, 4), (6.5, 4), (8, 5.5), (14, 5.5), (14, 13), (2, 13), close=True))


def _plus_at(x: float, y: float, r: float = 2.5) -> Glyph:
    return _lines((x - r, y, x + r, y), (x, y - r, x, y + r))


def _circle(cx: float, cy: float, r: float) -> Glyph:
    def draw(p: QPainter) -> None:
        p.drawEllipse(QPointF(cx, cy), r, r)

    return draw


def _dot(cx: float, cy: float, r: float = 1.0) -> Glyph:
    def draw(p: QPainter) -> None:
        brush = p.brush()
        p.setBrush(p.pen().color())
        p.drawEllipse(QPointF(cx, cy), r, r)
        p.setBrush(brush)

    return draw


def _all(*glyphs: Glyph) -> Glyph:
    def draw(p: QPainter) -> None:
        for glyph in glyphs:
            glyph(p)

    return draw


def _rect(x: float, y: float, w: float, h: float, r: float = 1.5) -> Glyph:
    def draw(p: QPainter) -> None:
        p.drawRoundedRect(QRectF(x, y, w, h), r, r)

    return draw


def _arc(cx: float, cy: float, r: float, start: float, span: float) -> Glyph:
    def draw(p: QPainter) -> None:
        p.drawArc(QRectF(cx - r, cy - r, 2 * r, 2 * r), int(start * 16), int(span * 16))

    return draw


GLYPHS: dict[str, Glyph] = {
    "sidebar": _all(_rect(2, 3, 12, 10), _lines((6, 3, 6, 13))),
    "note": _note,
    "new_note": _all(_note, _plus_at(8.5, 9.5, 2)),
    "folder": _folder,
    "new_folder": _all(_folder, _plus_at(8, 9.2, 2)),
    "add_folder": _all(_folder, _lines((8, 7.5, 8, 11), (6.3, 9.3, 8, 11), (9.7, 9.3, 8, 11))),
    "search": _all(_circle(7, 7, 4.2), _lines((10.2, 10.2, 13.5, 13.5))),
    "back": _paths(_path((10, 3), (5, 8), (10, 13))),
    "forward": _paths(_path((6, 3), (11, 8), (6, 13))),
    "chevron_right": _paths(_path((6.5, 4), (10.5, 8), (6.5, 12))),
    "chevron_down": _paths(_path((4, 6.5), (8, 10.5), (12, 6.5))),
    "close": _lines((4, 4, 12, 12), (12, 4, 4, 12)),
    "minimize": _lines((3.5, 8.5, 12.5, 8.5)),
    "maximize": _rect(3.5, 3.5, 9, 9, 1),
    "restore": _all(
        _rect(3.5, 5.5, 7, 7, 1),
        _paths(_path((5.5, 5.5), (5.5, 3.5), (12.5, 3.5), (12.5, 10.5), (10.5, 10.5))),
    ),
    "settings": _all(
        _lines((2.5, 4.5, 13.5, 4.5), (2.5, 8, 13.5, 8), (2.5, 11.5, 13.5, 11.5)),
        _dot(5.5, 4.5, 1.6),
        _dot(10.5, 8, 1.6),
        _dot(7, 11.5, 1.6),
    ),
    "help": _all(
        _circle(8, 8, 6),
        _paths(_path((6.2, 6.3), (6.6, 5.2), (8, 4.7), (9.5, 5.2), (9.8, 6.4), (8, 7.8), (8, 9.2))),
        _dot(8, 11.3, 0.9),
    ),
    "history": _all(_circle(8, 8, 6), _paths(_path((8, 4.5), (8, 8), (10.5, 9.5)))),
    "diff": _all(_lines((8, 2.5, 8, 13.5)), _plus_at(4.5, 6, 2), _lines((9.5, 10, 13.5, 10))),
    "pin": _all(
        _paths(_path((6, 2.5), (10, 2.5), (9.5, 7), (12, 9.5), (4, 9.5), (6.5, 7), close=True)),
        _lines((8, 9.5, 8, 14)),
    ),
    "more": _all(_dot(3.5, 8), _dot(8, 8), _dot(12.5, 8)),
    "check": _paths(_path((3.5, 8.5), (6.5, 11.5), (12.5, 4.5))),
    "sync_ok": _all(_circle(8, 8, 6), _paths(_path((5.3, 8.2), (7.2, 10.1), (10.8, 6.2)))),
    "sync_pending": _all(_arc(8, 8, 5.5, 60, 280), _paths(_path((12.5, 2.5), (11, 5.5), (8, 4.5)))),
    "sync_offline": _all(_circle(8, 8, 6), _lines((3.8, 12.2, 12.2, 3.8))),
    "warning": _all(
        _paths(_path((8, 2.5), (14, 13), (2, 13), close=True)), _lines((8, 6.5, 8, 9.5)), _dot(8, 11.3, 0.8)
    ),
    "error": _all(_circle(8, 8, 6), _lines((5.8, 5.8, 10.2, 10.2), (10.2, 5.8, 5.8, 10.2))),
    "info": _all(_circle(8, 8, 6), _lines((8, 7.3, 8, 11)), _dot(8, 5, 0.8)),
    "trash": _all(
        _lines((2.5, 4.5, 13.5, 4.5)),
        _paths(_path((4, 4.5), (5, 14), (11, 14), (12, 4.5))),
        _paths(_path((6, 4.5), (6.5, 2.5), (9.5, 2.5), (10, 4.5))),
    ),
    "rename": _all(
        _paths(_path((3, 13), (3.5, 10), (10.5, 3), (13, 5.5), (6, 12.5), close=True)),
        _lines((9, 4.5, 11.5, 7)),
    ),
    "copy": _all(
        _rect(5.5, 5.5, 8, 8), _paths(_path((10.5, 3.5), (10.5, 2.5), (2.5, 2.5), (2.5, 10.5), (3.5, 10.5)))
    ),
    "open": _all(
        _paths(_path((7, 3), (3, 3), (3, 13), (13, 13), (13, 9))),
        _lines((9, 3, 13, 3), (13, 3, 13, 7), (13, 3, 7.5, 8.5)),
    ),
    "plus": _plus_at(8, 8, 5),
    "inline": _lines((3, 4, 13, 4), (3, 7, 13, 7), (3, 10, 13, 10), (3, 13, 10, 13)),
    "side_by_side": _all(_rect(2.5, 3, 5, 10, 1), _rect(8.5, 3, 5, 10, 1)),
    "up": _paths(_path((4, 10), (8, 6), (12, 10))),
    "down": _paths(_path((4, 6), (8, 10), (12, 6))),
    "home": _all(
        _paths(_path((2.5, 7.5), (8, 2.8), (13.5, 7.5))),
        _paths(_path((4, 6.5), (4, 13.5), (12, 13.5), (12, 6.5))),
        _lines((6.8, 13.5, 6.8, 10), (6.8, 10, 9.2, 10), (9.2, 10, 9.2, 13.5)),
    ),
    "command": _all(_paths(_path((3, 4.5), (6.5, 8), (3, 11.5))), _lines((8, 11.5, 13, 11.5))),
    "tag": _all(
        _paths(_path((2.5, 2.5), (8, 2.5), (13.5, 8), (8, 13.5), (2.5, 8), close=True)),
        _dot(5.8, 5.8, 1.1),
    ),
    "heading": _lines((4, 3, 4, 13), (12, 3, 12, 13), (4, 8, 12, 8)),
    "line": _all(_lines((2.5, 5, 6, 5), (2.5, 8, 13.5, 8), (2.5, 11, 9, 11))),
    "logo": _all(
        _paths(_path((3.5, 2), (9.5, 2), (12.5, 5), (12.5, 14), (3.5, 14), close=True)),
        _lines((6, 7.5, 10, 7.5), (6, 10, 10, 10)),
        _paths(_path((9.5, 2), (9.5, 5), (12.5, 5))),
    ),
    "library": _all(_rect(2.5, 2.5, 3, 11, 0.8), _rect(6.5, 2.5, 3, 11, 0.8), _lines((11, 3.2, 13.6, 12.8))),
    "tabs": _all(_rect(2.5, 5, 11, 8, 1.2), _paths(_path((2.5, 5), (2.5, 3), (7, 3), (7, 5)))),
    "keyboard": _all(
        _rect(1.5, 4, 13, 8, 1.5),
        _lines((4, 10, 12, 10)),
        _dot(4.5, 7, 0.7),
        _dot(7, 7, 0.7),
        _dot(9.5, 7, 0.7),
        _dot(12, 7, 0.7),
    ),
}


def draw_glyph(painter: QPainter, name: str, rect: QRectF, color: QColor, stroke: float = 1.5) -> None:
    glyph = GLYPHS[name]
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.translate(rect.topLeft())
    painter.scale(rect.width() / 16, rect.height() / 16)
    pen = QPen(color, stroke)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    glyph(painter)
    painter.restore()


class GlyphEngine(QIconEngine):
    def __init__(self, name: str, color: str, active: str, disabled: str) -> None:
        super().__init__()
        if name not in GLYPHS:
            raise KeyError(name)
        self._name = name
        self._colors = {
            QIcon.Mode.Normal: QColor(color),
            QIcon.Mode.Selected: QColor(active),
            QIcon.Mode.Active: QColor(active),
            QIcon.Mode.Disabled: QColor(disabled),
        }

    def paint(self, painter: QPainter, rect: QRect, mode: QIcon.Mode, state: QIcon.State) -> None:
        side = min(rect.width(), rect.height())
        square = QRectF(
            rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2, side, side
        )
        draw_glyph(painter, self._name, square, self._colors.get(mode, self._colors[QIcon.Mode.Normal]))

    def pixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State) -> QPixmap:
        return self.scaledPixmap(size, mode, state, 1.0)

    def scaledPixmap(self, size: QSize, mode: QIcon.Mode, state: QIcon.State, scale: float) -> QPixmap:  # noqa: N802
        pixmap = QPixmap(max(1, round(size.width() * scale)), max(1, round(size.height() * scale)))
        pixmap.setDevicePixelRatio(scale)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, QRect(0, 0, size.width(), size.height()), mode, state)
        painter.end()
        return pixmap

    def clone(self) -> QIconEngine:
        colors = self._colors
        return GlyphEngine(
            self._name,
            colors[QIcon.Mode.Normal].name(),
            colors[QIcon.Mode.Active].name(),
            colors[QIcon.Mode.Disabled].name(),
        )


def glyph_icon(name: str, color: str, active: str | None = None, disabled: str | None = None) -> QIcon:
    return QIcon(GlyphEngine(name, color, active or color, disabled or color))
