"""Restrained toasts for completed actions and automatic updates. Decisions never live in a toast."""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject, QTimer
from PySide6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QLabel, QToolButton, QWidget

from paperless_notes.ui.shell.motion import EASE_IN, EASE_OUT, Tween
from paperless_notes.ui.shell.widgets import FloatingPanel
from paperless_notes.ui.theme.icons import glyph_icon
from paperless_notes.ui.theme.tokens import Theme

MAX_KEPT = 50
RISE = 16


class Toast(FloatingPanel):
    """One message at a time, bottom centre of its host. It slides up and fades in, stays for the theme's
    toast duration, then slides back down and fades out."""

    def __init__(self, host: QWidget, theme: Theme) -> None:
        super().__init__(host, theme)
        self._host = host
        self.messages: list[str] = []
        layout = QHBoxLayout(self)
        s = theme.spacing
        layout.setContentsMargins(*self.inner_margins(s.lg, s.sm))
        layout.setSpacing(s.md)
        self.label = QLabel("")
        self.label.setWordWrap(True)
        layout.addWidget(self.label, 1)
        self.close_button = QToolButton()
        self.close_button.setAccessibleName("Close message")
        self.close_button.setToolTip("Close this message")
        self.close_button.clicked.connect(self.dismiss)
        layout.addWidget(self.close_button)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.dismiss)
        self._rest = 0
        self._shown = Tween(self, 0.0, self._step)
        host.installEventFilter(self)
        self.apply_theme(theme)
        self.hide()

    def apply_theme(self, theme: Theme) -> None:
        super().apply_theme(theme)
        p = theme.palette
        self.close_button.setIcon(glyph_icon("close", p.text_secondary, p.text))

    def show_message(self, text: str) -> None:
        self.messages.append(text)
        del self.messages[:-MAX_KEPT]
        self.label.setText(text)
        self.setAccessibleName(text)
        self._place()
        if not self.isVisible():
            self._shown.jump(0.0)
        self.show()
        self.raise_()
        self._animate(1.0)
        self._timer.start(self._theme.motion.toast_ms)

    def dismiss(self) -> None:
        """Slide the message away; it hides when the slide ends."""
        self._timer.stop()
        if self.isVisible():
            self._animate(0.0)

    def _animate(self, target: float) -> None:
        if self._shown.target == target and (self._shown.running() or self._shown.value == target):
            return
        duration = self._theme.ms(self._theme.motion.normal_ms)
        if duration > 0 and not isinstance(self.graphicsEffect(), QGraphicsOpacityEffect):
            self.setGraphicsEffect(QGraphicsOpacityEffect(self))
        self._shown.to(target, duration, EASE_OUT if target else EASE_IN, self._settled)

    def _step(self, value: float) -> None:
        self.move(self.x(), self._rest + round((1.0 - float(value)) * RISE))
        effect = self.graphicsEffect()
        if isinstance(effect, QGraphicsOpacityEffect):
            effect.setOpacity(float(value))

    def _settled(self) -> None:
        self.setGraphicsEffect(None)  # type: ignore[arg-type]
        if self._shown.value == 0.0:
            self.hide()

    def _place(self) -> None:
        s = self._theme.spacing
        width = min(480, max(240, self._host.width() - 4 * s.xxl)) + 2 * self.SHADOW
        self.setFixedWidth(width)
        self.adjustSize()
        x = (self._host.width() - width) // 2
        self._rest = max(0, self._host.height() - self.height() - s.xl - self._theme.metrics.status_bar)
        self.move(max(0, x), self._rest + round((1.0 - float(self._shown.value)) * RISE))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self._host and event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False
