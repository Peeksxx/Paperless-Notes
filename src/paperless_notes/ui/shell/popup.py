"""Small surfaces that open beside something in their host, such as a word in the editor. They fade in while
moving a few pixels towards it and fade out the same way. Durations come from the theme, so with Reduce
motion they appear and disappear at once."""

from __future__ import annotations

from PySide6.QtCore import QPoint, QRect, Qt
from PySide6.QtWidgets import QGraphicsOpacityEffect, QListWidget, QWidget

from paperless_notes.ui.shell.motion import EASE_IN, EASE_OUT, Tween, clear_effect
from paperless_notes.ui.shell.widgets import FloatingPanel
from paperless_notes.ui.theme.tokens import Theme

DRIFT = 4
MARGIN = 4


def fit_list(rows: QListWidget, area: QRect, max_rows: int, min_width: int = 220) -> None:
    """Size a popup's list for up to ``max_rows`` of its rows, never wider or taller than ``area`` allows."""
    count = min(rows.count(), max_rows)
    row = max(rows.sizeHintForRow(0), 24)
    width = max(min_width, rows.sizeHintForColumn(0) + 32)
    room = 2 * (FloatingPanel.SHADOW + MARGIN)
    rows.setFixedSize(
        max(1, min(width, area.width() - room)),
        max(1, min(count * row + 2 * rows.frameWidth(), area.height() - room)),
    )


class Popup(FloatingPanel):
    """A floating panel placed against an anchor rectangle given in its host's coordinates: below the anchor
    when it fits, otherwise above, and always inside the host."""

    def __init__(self, host: QWidget, theme: Theme) -> None:
        super().__init__(host, theme)
        self._host = host
        self._rest = QPoint()
        self._drift = DRIFT
        self._shown = Tween(self, 0.0, self._step)
        self.hide()

    @property
    def is_open(self) -> bool:
        """Shown or opening; False as soon as closing begins."""
        return self.isVisible() and self._shown.target == 1.0

    def present(
        self, anchor: QRect, gap: int = MARGIN, above: bool = False, bounds: QRect | None = None
    ) -> None:
        """Open against ``anchor``, or follow it when already open: below it, or above it when ``above`` asks
        for that or there is no room below, inside ``bounds`` (the host by default)."""
        self.adjustSize()
        self._rest = self._spot(anchor, gap, above, bounds if bounds is not None else self._host.rect())
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, False)
        if not self.isVisible():
            self._shown.jump(0.0)
            if self._duration() > 0:
                self.setGraphicsEffect(QGraphicsOpacityEffect(self))
            self._step(0.0)
            self.show()
        self.raise_()
        self._animate(1.0)
        self._step(self._shown.value)

    def dismiss(self) -> None:
        """Fade out; the popup hides when the fade ends and takes no clicks meanwhile."""
        if self.isVisible():
            self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            self._animate(0.0)

    def close_now(self) -> None:
        self._shown.jump(0.0)
        clear_effect(self)
        self.hide()

    def _duration(self) -> int:
        return self._theme.ms(self._theme.motion.fast_ms)

    def _animate(self, target: float) -> None:
        if self._shown.target == target and (self._shown.running() or self._shown.value == target):
            return
        duration = self._duration()
        if duration > 0 and not isinstance(self.graphicsEffect(), QGraphicsOpacityEffect):
            self.setGraphicsEffect(QGraphicsOpacityEffect(self))
        self._shown.to(target, duration, EASE_OUT if target else EASE_IN, self._settled)

    def _step(self, value: float) -> None:
        self.move(self._rest.x(), self._rest.y() + round((1.0 - float(value)) * self._drift))
        effect = self.graphicsEffect()
        if isinstance(effect, QGraphicsOpacityEffect):
            effect.setOpacity(float(value))

    def _settled(self) -> None:
        clear_effect(self)
        if self._shown.value == 0.0:
            self.hide()

    def _spot(self, anchor: QRect, gap: int, above: bool, bounds: QRect) -> QPoint:
        """Where the widget goes so its visible panel, inside the shadow margins, sits against ``anchor``."""
        s = self.SHADOW
        top_shadow, bottom_shadow = s - 4, s + 4
        panel_height = self.height() - top_shadow - bottom_shadow
        left, right = bounds.left() + MARGIN, bounds.right() + 1 - MARGIN
        top, bottom = bounds.top() + MARGIN, bounds.bottom() + 1 - MARGIN
        x = max(left - s, min(anchor.left() - s, right - self.width() + s))
        under = anchor.bottom() + 1 + gap
        over = anchor.top() - gap - panel_height
        fits_under, fits_over = under + panel_height <= bottom, over >= top
        if fits_over and (above or not fits_under):
            self._drift = -DRIFT
            return QPoint(x, over - top_shadow)
        self._drift = DRIFT
        y = max(top, min(under, bottom - panel_height)) if not fits_under else under
        return QPoint(x, y - top_shadow)
