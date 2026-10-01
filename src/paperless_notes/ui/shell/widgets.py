"""Painted primitives shared by the shell: keycaps, row buttons, window caption buttons, section labels, state
dots and a soft shadow. Icons, text and shortcuts are placed from font metrics so they share one baseline at
every scale."""

from __future__ import annotations

from collections.abc import Sequence

from PySide6.QtCore import QPointF, QRect, QRectF, QSize, Qt
from PySide6.QtGui import (
    QColor,
    QEnterEvent,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPen,
)
from PySide6.QtWidgets import QAbstractButton, QFrame, QHBoxLayout, QLabel, QSizePolicy, QWidget

from paperless_notes.ui.shell.motion import Glow, Tween, faded, mix
from paperless_notes.ui.theme.icons import draw_glyph
from paperless_notes.ui.theme.tokens import Palette, Theme

OK, BUSY, ATTENTION, IDLE = "ok", "busy", "attention", "idle"
_KEY_NAMES = {
    "Return": "Enter",
    "Del": "Delete",
    "Left": "\N{LEFTWARDS ARROW}",
    "Right": "\N{RIGHTWARDS ARROW}",
}
_KEY_NAMES |= {"Up": "\N{UPWARDS ARROW}", "Down": "\N{DOWNWARDS ARROW}"}


def meta_font(theme: Theme, point_size: float | None = None, bold: bool = False) -> QFont:
    """The interface face at its small optical size, for captions, paths, times, counts and keys."""
    t = theme.typography
    font = QFont()
    font.setFamilies(list(t.ui_families))
    font.setPointSizeF(point_size or t.meta_pt)
    tag = QFont.Tag.fromString("opsz")
    if tag is not None:
        font.setVariableAxis(tag, t.meta_opsz)
    if bold:
        font.setWeight(QFont.Weight.DemiBold)
    return font


def caption_font(theme: Theme) -> QFont:
    font = meta_font(theme, bold=True)
    font.setCapitalization(QFont.Capitalization.AllUppercase)
    font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.6)
    return font


def display_font(theme: Theme, point_size: float | None = None) -> QFont:
    t = theme.typography
    font = QFont()
    font.setFamilies(list(t.display_families))
    font.setPointSizeF(point_size or t.display_pt)
    font.setWeight(QFont.Weight.DemiBold)
    tag = QFont.Tag.fromString("opsz")
    if tag is not None:
        font.setVariableAxis(tag, t.display_opsz)
    return font


def tone_color(tone: str, p: Palette) -> str:
    return {OK: p.state_ok, BUSY: p.state_busy, ATTENTION: p.state_attention}.get(tone, p.marker)


def tone_text_color(tone: str, p: Palette, neutral: str) -> str:
    """Text colour for a state: the warning colour while saving, the error colour when a note needs
    attention, otherwise ``neutral``."""
    return {BUSY: p.warning_text, ATTENTION: p.error_text}.get(tone, neutral)


class ToneLabel(QLabel):
    """A label whose text colour shows a state instead of a dot; the colour fades between states."""

    def __init__(self, theme: Theme, neutral: str = "text_muted") -> None:
        super().__init__()
        self._theme = theme
        self._neutral = neutral
        self.tone = ""
        self._color = Tween(self, QColor(self._target()), lambda _value: self.update())

    def _target(self) -> str:
        p = self._theme.palette
        return tone_text_color(self.tone, p, str(getattr(p, self._neutral)))

    def set_tone(self, tone: str) -> None:
        if tone != self.tone:
            self.tone = tone
            self._color.to(QColor(self._target()), self._theme.ms(self._theme.motion.normal_ms))

    def color(self) -> QColor:
        return QColor(self._color.value)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self._color.jump(QColor(self._target()))

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        painter.setFont(self.font())
        painter.setPen(self.color())
        rect = self.contentsRect()
        text = self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, rect.width())
        painter.drawText(rect, int(self.alignment()), text)


def paint_dot(painter: QPainter, center: QPointF, tone: str, p: Palette, radius: float = 3.5) -> None:
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(tone_color(tone, p)))
    painter.drawEllipse(center, radius, radius)
    painter.restore()


def key_parts(shortcut: str) -> list[str]:
    """``Ctrl+Shift+P`` as ``["Ctrl", "Shift", "P"]``; a key that is itself ``+`` or ``-`` survives."""
    parts: list[str] = []
    for piece in shortcut.split("+"):
        if piece:
            parts.append(_KEY_NAMES.get(piece, piece))
        elif parts and parts[-1] != "+":
            parts.append("+")
    return parts


def keycaps_width(shortcut: str, font: QFont) -> int:
    parts = key_parts(shortcut)
    if not parts:
        return 0
    metrics = QFontMetrics(font)
    return sum(max(metrics.horizontalAdvance(k) + 10, 18) for k in parts) + 3 * (len(parts) - 1)


def paint_keycaps(
    painter: QPainter,
    right: float,
    center_y: float,
    shortcut: str,
    font: QFont,
    p: Palette,
    align_left: bool = False,
) -> int:
    """Draw ``shortcut`` as small keycaps ending at ``right`` (or starting there when ``align_left``)."""
    parts = key_parts(shortcut)
    if not parts:
        return 0
    metrics = QFontMetrics(font)
    total = keycaps_width(shortcut, font)
    x = right if align_left else right - total
    height = max(18, metrics.height() + 4)
    top = round(center_y - height / 2)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setFont(font)
    for part in parts:
        width = max(metrics.horizontalAdvance(part) + 10, 18)
        rect = QRectF(x + 0.5, top + 0.5, width - 1, height - 1)
        painter.setPen(QPen(QColor(p.marker), 1))
        painter.setBrush(QColor(p.window))
        painter.drawRoundedRect(rect, 4, 4)
        painter.setPen(QColor(p.text_secondary))
        painter.drawText(QRectF(x, top, width, height - 1), Qt.AlignmentFlag.AlignCenter, part)
        x += width + 3
    painter.restore()
    return total


def paint_shadow(
    painter: QPainter, rect: QRectF, radius: float, color: str, depth: int = 14, strength: float = 0.5
) -> None:
    """A soft shadow under ``rect``: stacked translucent rounded rectangles, denser near the edge."""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    base = QColor(color)
    for i in range(depth, 0, -1):
        falloff = (1 - i / (depth + 1)) ** 2
        base.setAlphaF(min(1.0, strength * falloff / depth * 2.2))
        painter.setBrush(base)
        grown = rect.adjusted(-i, -i + depth * 0.25, i, i + depth * 0.25)
        painter.drawRoundedRect(grown, radius + i, radius + i)
    painter.restore()


class FloatingPanel(QFrame):
    """A surface floating over the window: rounded, hairline border and a soft painted shadow. Content goes
    inside ``inner_margins`` so it never covers the shadow."""

    SHADOW = 14

    def __init__(self, host: QWidget, theme: Theme) -> None:
        super().__init__(host)
        self.setObjectName("Floating")
        self._theme = theme
        self._opacity = 1.0

    def inner_margins(self, horizontal: int, vertical: int) -> tuple[int, int, int, int]:
        s = self.SHADOW
        return (s + horizontal, s - 4 + vertical, s + horizontal, s + 4 + vertical)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        t = self._theme
        p = t.palette
        s = self.SHADOW
        painter = QPainter(self)
        panel = QRectF(self.rect()).adjusted(s, s - 4, -s, -s - 4)
        paint_shadow(painter, panel, t.radius.lg, p.shadow, depth=s, strength=0.5 if t.dark else 0.24)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(p.border_strong if t.dark else p.border), 1))
        painter.setBrush(QColor(p.surface))
        painter.drawRoundedRect(panel.adjusted(0.5, 0.5, -0.5, -0.5), t.radius.lg, t.radius.lg)


class Keycaps(QWidget):
    def __init__(self, shortcut: str, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._shortcut = shortcut
        self._theme = theme
        self.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        self.setAccessibleName(shortcut)

    def set_shortcut(self, shortcut: str) -> None:
        self._shortcut = shortcut
        self.setAccessibleName(shortcut)
        self.updateGeometry()
        self.update()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        font = meta_font(self._theme)
        return QSize(keycaps_width(self._shortcut, font) + 1, max(20, QFontMetrics(font).height() + 6))

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        paint_keycaps(
            painter, 0, self.height() / 2, self._shortcut, meta_font(self._theme), self._theme.palette, True
        )


class RowButton(QAbstractButton):
    """A full-width row: glyph, label and an optional shortcut shown as keycaps or a small annotation."""

    def __init__(
        self,
        text: str,
        glyph: str,
        theme: Theme,
        shortcut: str = "",
        annotation: str = "",
        parent: QWidget | None = None,
        framed: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setText(text)
        self.setAccessibleName(text)
        self._glyph = glyph
        self._shortcut = shortcut
        self._annotation = annotation
        self._theme = theme
        self._hover = False
        self._glow = Glow(self, lambda: self._theme.ms(self._theme.motion.fast_ms))
        self._framed = framed
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def set_annotation(self, text: str) -> None:
        self._annotation = text
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        m = self._theme.metrics
        self.ensurePolished()
        # 36 before the label and 8 after it, plus 4 so rounding never elides a label that fits.
        width = 48 + self.fontMetrics().horizontalAdvance(self.text())
        if self._shortcut:
            width += keycaps_width(self._shortcut, meta_font(self._theme)) + 12
        return QSize(width, m.row)

    def enterEvent(self, event: QEnterEvent) -> None:  # noqa: N802 - Qt override
        self._hover = True
        self._glow.set_on(True)
        super().enterEvent(event)

    def leaveEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        self._hover = False
        self._glow.set_on(False)
        super().leaveEvent(event)  # type: ignore[arg-type]

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        p = self._theme.palette
        glow = self._glow.amount
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        rest = QColor(p.surface if self._theme.dark else p.page) if self._framed else faded(p.hover, 0.0)
        fill = QColor(p.selected) if self.isDown() or self.isChecked() else mix(rest, p.hover, glow)
        if fill.alpha() > 0:
            painter.setPen(
                QPen(mix(p.border, p.border_strong, glow), 1) if self._framed else Qt.PenStyle.NoPen
            )
            painter.setBrush(fill)
            painter.drawRoundedRect(rect, self._theme.radius.md, self._theme.radius.md)
        if self.hasFocus() and self.window().testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange):
            painter.setPen(QPen(QColor(p.focus), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(
                rect.adjusted(0.75, 0.75, -0.75, -0.75), self._theme.radius.md, self._theme.radius.md
            )
        color = mix(p.text_secondary, p.text, 1.0 if self.isChecked() else glow)
        if not self.isEnabled():
            color = QColor(p.text_muted)
        mid = self.height() / 2
        draw_glyph(painter, self._glyph, QRectF(10, mid - 8, 16, 16), color)
        right = self.width() - 8.0
        if self._shortcut:
            right -= paint_keycaps(painter, right, mid, self._shortcut, meta_font(self._theme), p) + 8
        elif self._annotation:
            font = meta_font(self._theme)
            painter.setFont(font)
            painter.setPen(QColor(p.text_muted))
            width = QFontMetrics(font).horizontalAdvance(self._annotation)
            painter.drawText(
                QRectF(right - width, 0, width, self.height()),
                Qt.AlignmentFlag.AlignVCenter,
                self._annotation,
            )
            right -= width + 8
        painter.setFont(self.font())
        painter.setPen(color)
        text_rect = QRectF(36, 0, max(0.0, right - 36), self.height())
        painter.drawText(
            text_rect,
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self.fontMetrics().elidedText(self.text(), Qt.TextElideMode.ElideRight, int(text_rect.width())),
        )


class CaptionButton(QAbstractButton):
    """Minimize, maximize or restore, and close, drawn like the Windows 11 caption buttons: equal size,
    full bar height, 10 px glyphs snapped to device pixels, red only under the pointer on Close."""

    def __init__(self, kind: str, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.kind = kind
        self._theme = theme
        self._hover = False
        self._glow = Glow(self, lambda: self._theme.ms(self._theme.motion.fast_ms))
        self._active = True
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFixedSize(theme.metrics.caption_button, theme.metrics.title_bar)

    def set_kind(self, kind: str) -> None:
        self.kind = kind
        self.update()

    def set_window_active(self, active: bool) -> None:
        self._active = active
        self.update()

    def set_hover(self, hover: bool) -> None:
        """Hover from outside Qt's own events, for a button Windows treats as part of the frame."""
        if hover != self._hover:
            self._hover = hover
            self._glow.set_on(hover)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.setFixedSize(theme.metrics.caption_button, theme.metrics.title_bar)
        self.update()

    def enterEvent(self, event: QEnterEvent) -> None:  # noqa: N802 - Qt override
        self.set_hover(True)
        super().enterEvent(event)

    def leaveEvent(self, event: object) -> None:  # noqa: N802 - Qt override
        self.set_hover(False)
        super().leaveEvent(event)  # type: ignore[arg-type]

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        p = self._theme.palette
        glow = self._glow.amount
        painter = QPainter(self)
        close = self.kind == "close"
        glyph = QColor(p.text if self._active else p.text_muted)
        if self.isDown():
            painter.fillRect(self.rect(), QColor(p.danger_pressed if close else p.selected))
            glyph = QColor(p.on_danger) if close else glyph
        elif glow > 0:
            painter.fillRect(self.rect(), faded(p.danger_fill if close else p.hover, glow))
            glyph = mix(glyph, p.on_danger if close else p.text, glow)
        dpr = self.devicePixelRatioF()
        side = round(10 * dpr)
        stroke = max(1, round(dpr))
        cx = round(self.width() * dpr / 2)
        cy = round(self.height() * dpr / 2)
        x0, y0 = cx - side // 2, cy - side // 2
        half = stroke / 2
        painter.save()
        painter.scale(1 / dpr, 1 / dpr)
        pen = QPen(glyph, stroke)
        pen.setCapStyle(Qt.PenCapStyle.FlatCap)
        pen.setJoinStyle(Qt.PenJoinStyle.MiterJoin)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.kind == "minimize":
            painter.drawLine(QPointF(x0, cy + half), QPointF(x0 + side, cy + half))
        elif self.kind == "maximize":
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            painter.drawRoundedRect(QRectF(x0 + half, y0 + half, side - stroke, side - stroke), dpr, dpr)
        elif self.kind == "restore":
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            step = round(2 * dpr)
            front = QRectF(x0 + half, y0 + step + half, side - step - stroke, side - step - stroke)
            painter.drawRoundedRect(front, dpr, dpr)
            back = QPainterPath(QPointF(x0 + step + half, y0 + step))
            back.lineTo(QPointF(x0 + step + half, y0 + half))
            back.lineTo(QPointF(x0 + side - half, y0 + half))
            back.lineTo(QPointF(x0 + side - half, y0 + side - step - half))
            back.lineTo(QPointF(x0 + side - step, y0 + side - step - half))
            painter.drawPath(back)
        else:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawLine(QPointF(x0, y0), QPointF(x0 + side, y0 + side))
            painter.drawLine(QPointF(x0 + side, y0), QPointF(x0, y0 + side))
        painter.restore()


class SectionLabel(QWidget):
    """An uppercase caption followed by a hairline to the trailing controls, like a drawing's title block."""

    def __init__(
        self, text: str, theme: Theme, trailing: Sequence[QWidget] = (), parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._theme = theme
        self.setAccessibleName(text)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.spacing.xxs)
        layout.addStretch(1)
        self._trailing = list(trailing)
        for widget in self._trailing:
            layout.addWidget(widget)
        self.setMinimumHeight(24)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def text(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text
        self.setAccessibleName(text)
        self.update()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        font = caption_font(self._theme)
        extra = sum(w.sizeHint().width() for w in self._trailing)
        return QSize(QFontMetrics(font).horizontalAdvance(self._text) + 40 + extra, 24)

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        p = self._theme.palette
        painter = QPainter(self)
        font = caption_font(self._theme)
        painter.setFont(font)
        painter.setPen(QColor(p.text_muted))
        width = QFontMetrics(font).horizontalAdvance(self._text)
        margins = self.contentsMargins()
        painter.drawText(
            QRect(margins.left(), 0, width + 4, self.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self._text,
        )
        end = self.width() - margins.right()
        visible = [w for w in self._trailing if w.isVisible()]
        if visible:
            end = min(w.geometry().left() for w in visible) - 6
        start = margins.left() + width + 10
        if end > start:
            painter.setPen(QPen(QColor(p.border), 1))
            y = self.height() // 2 + 0.5
            painter.drawLine(QPointF(start, y), QPointF(end, y))
