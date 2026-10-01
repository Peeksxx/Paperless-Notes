"""Animation helpers for the shell. Durations come from the theme, so with Reduce motion (duration 0) every
change applies its end state at once and no timer runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QAbstractAnimation, QEasingCurve, QEvent, QObject, QSize, Qt, QVariantAnimation
from PySide6.QtGui import QColor, QResizeEvent
from PySide6.QtWidgets import QGraphicsOpacityEffect, QWidget

EASE_OUT = QEasingCurve.Type.OutCubic
EASE_IN = QEasingCurve.Type.InCubic
EASE_IN_OUT = QEasingCurve.Type.InOutCubic


def animate(
    owner: QObject,
    start: Any,
    end: Any,
    duration: int,
    apply: Callable[[Any], None],
    done: Callable[[], None] | None = None,
    curve: QEasingCurve.Type = EASE_OUT,
) -> QVariantAnimation | None:
    """Run ``apply`` with values from ``start`` to ``end``; returns None when nothing animates."""
    if duration <= 0:
        apply(end)
        if done is not None:
            done()
        return None
    animation = QVariantAnimation(owner)
    animation.setStartValue(start)
    animation.setEndValue(end)
    animation.setDuration(duration)
    animation.setEasingCurve(curve)
    animation.valueChanged.connect(apply)
    if done is not None:
        animation.finished.connect(done)
    animation.start(QAbstractAnimation.DeletionPolicy.DeleteWhenStopped)
    return animation


class Tween(QObject):
    """One animated value that can be sent to a new target at any time; it continues from where it is."""

    def __init__(self, owner: QObject, value: Any, apply: Callable[[Any], None]) -> None:
        super().__init__(owner)
        self.value = value
        self.target = value
        self._apply = apply
        self._animation: QVariantAnimation | None = None

    def running(self) -> bool:
        return self._animation is not None

    def to(
        self,
        target: Any,
        duration: int,
        curve: QEasingCurve.Type = EASE_OUT,
        done: Callable[[], None] | None = None,
    ) -> None:
        self.stop()
        self.target = target
        if duration <= 0 or target == self.value:
            self._set(target)
            if done is not None:
                done()
            return

        def finished() -> None:
            self._animation = None
            if done is not None:
                done()

        self._animation = animate(self, self.value, target, duration, self._set, finished, curve)

    def jump(self, value: Any) -> None:
        self.stop()
        self.target = value
        self._set(value)

    def stop(self) -> None:
        if self._animation is not None:
            animation, self._animation = self._animation, None
            animation.stop()

    def _set(self, value: Any) -> None:
        self.value = value
        self._apply(value)


def fade_in(widget: QWidget, duration: int, rise: int = 0) -> None:
    """Fade a just-shown floating widget in, rising ``rise`` pixels into its place."""
    if duration <= 0:
        return
    rest = widget.pos()
    effect = QGraphicsOpacityEffect(widget)
    effect.setOpacity(0.0)
    widget.setGraphicsEffect(effect)

    def step(value: float) -> None:
        if widget.graphicsEffect() is effect:
            effect.setOpacity(float(value))
            widget.move(rest.x(), rest.y() + round((1.0 - float(value)) * rise))

    def done() -> None:
        if widget.graphicsEffect() is effect:
            widget.setGraphicsEffect(None)  # type: ignore[arg-type]

    animate(widget, 0.0, 1.0, duration, step, done)


def mix(start: QColor | str, end: QColor | str, amount: float) -> QColor:
    """The colour ``amount`` (0 to 1) of the way from ``start`` to ``end``, alpha included."""
    a, b = QColor(start), QColor(end)
    t = max(0.0, min(1.0, amount))
    return QColor.fromRgbF(
        a.redF() + (b.redF() - a.redF()) * t,
        a.greenF() + (b.greenF() - a.greenF()) * t,
        a.blueF() + (b.blueF() - a.blueF()) * t,
        a.alphaF() + (b.alphaF() - a.alphaF()) * t,
    )


def faded(color: QColor | str, amount: float) -> QColor:
    """``color`` with its alpha scaled by ``amount``, for fills that fade in on hover."""
    result = QColor(color)
    result.setAlphaF(result.alphaF() * max(0.0, min(1.0, amount)))
    return result


class Glow(Tween):
    """A hover amount from 0 to 1 that fades with the theme's fast duration and repaints its widget."""

    def __init__(self, widget: QWidget, duration: Callable[[], int]) -> None:
        super().__init__(widget, 0.0, lambda _value: widget.update())
        self._duration = duration

    def set_on(self, on: bool) -> None:
        self.to(1.0 if on else 0.0, self._duration())

    @property
    def amount(self) -> float:
        return float(self.value)


class SlideOut(QWidget):
    """Holds one panel and shows or hides it by sliding it in from ``edge``. The panel keeps its own size and
    moves while the box grows or shrinks, so the neighbours in the layout give way smoothly instead of the
    panel squeezing. Settled, the box takes the panel's size limits and the panel fills it.

    The panel routes its own ``setVisible`` here through ``take``, so code that shows or hides the panel
    animates it; ``is_open`` reports the intent while an animation runs."""

    def __init__(self, content: QWidget, edge: Qt.Edge, duration: Callable[[], int]) -> None:
        super().__init__(content.parentWidget())
        self.content = content
        self._edge = edge
        self._horizontal = edge in (Qt.Edge.LeftEdge, Qt.Edge.RightEdge)
        self._duration = duration
        self._passing = False
        self._animating = False
        self._full = 0
        self._remembered = 0
        explicit = content.testAttribute(Qt.WidgetAttribute.WA_WState_ExplicitShowHide)
        self.target = not (content.isHidden() and explicit)
        self._progress = Tween(self, 1.0 if self.target else 0.0, self._step)
        self.setSizePolicy(content.sizePolicy())
        content.setParent(self)
        self._pass_visible()
        content.installEventFilter(self)
        self._settle()
        self.setVisible(self.target)

    @staticmethod
    def take(widget: QWidget, visible: bool) -> bool:
        """Called from a panel's ``setVisible``: True when the panel's box handles the change."""
        box = widget.parentWidget()
        if isinstance(box, SlideOut) and box.content is widget and not box._passing:
            box.set_open(visible)
            return True
        return False

    @staticmethod
    def is_open(widget: QWidget) -> bool:
        box = widget.parentWidget()
        if isinstance(box, SlideOut) and box.content is widget:
            return box.target
        return widget.isVisible()

    def running(self) -> bool:
        return self._animating

    def set_open(self, opened: bool) -> None:
        if opened == self.target and (self.running() or self.isVisible() == opened):
            return
        self.target = opened
        duration = self._duration()
        parent = self.parentWidget()
        if duration <= 0 or parent is None or not parent.isVisible():
            self._progress.jump(1.0 if opened else 0.0)
            self._finish()
            return
        if not self._animating:
            self._animating = True
            self._full = 0 if opened else self._extent()
            self._progress.jump(0.0 if opened else 1.0)
            self.setVisible(True)
            if opened:
                layout = parent.layout()
                if layout is not None:
                    layout.activate()
                self._full = self._natural()
        self._progress.to(1.0 if opened else 0.0, duration, done=self._finish)

    def _natural(self) -> int:
        low, high = self._limits()
        if self._horizontal:
            size = self._remembered or self.content.sizeHint().width()
        elif self.content.hasHeightForWidth() and self.width() > 0:
            size = self.content.heightForWidth(self.width())
        else:
            size = self.content.sizeHint().height()
        return max(low, min(high, size))

    def _extent(self) -> int:
        return self.width() if self._horizontal else self.height()

    def _limits(self) -> tuple[int, int]:
        c = self.content
        if self._horizontal:
            return c.minimumWidth(), c.maximumWidth()
        return c.minimumHeight(), c.maximumHeight()

    def _step(self, value: float) -> None:
        if not self._animating:
            return
        extent = round(self._full * float(value))
        if self._horizontal:
            self.setFixedWidth(extent)
        else:
            self.setFixedHeight(extent)
        self._place()

    def _finish(self) -> None:
        self._animating = False
        if not self.target:
            if self._horizontal and self._full > 0:
                self._remembered = self._full
            self.setVisible(False)
        self._settle()
        if self.target:
            self.setVisible(True)

    def _settle(self) -> None:
        low, high = self._limits()
        if self._horizontal:
            self.setMinimumWidth(low)
            self.setMaximumWidth(high)
        else:
            self.setMinimumHeight(low)
            self.setMaximumHeight(high)
        self.updateGeometry()
        self._place()

    def _pass_visible(self) -> None:
        self._passing = True
        try:
            self.content.setVisible(True)
        finally:
            self._passing = False

    def _place(self) -> None:
        if not self.running():
            self.content.setGeometry(self.rect())
            return
        if self._horizontal:
            x = self.width() - self._full if self._edge == Qt.Edge.LeftEdge else 0
            self.content.setGeometry(x, 0, self._full, self.height())
        else:
            y = self.height() - self._full if self._edge == Qt.Edge.TopEdge else 0
            self.content.setGeometry(0, y, self.width(), self._full)

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.content.sizeHint()

    def minimumSizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return self.content.minimumSizeHint()

    def hasHeightForWidth(self) -> bool:  # noqa: N802 - Qt override
        return not self._horizontal and not self.running() and self.content.hasHeightForWidth()

    def heightForWidth(self, width: int) -> int:  # noqa: N802 - Qt override
        return self.content.heightForWidth(width)

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._place()

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self.content and event.type() == QEvent.Type.LayoutRequest and not self.running():
            self.updateGeometry()
        return False
