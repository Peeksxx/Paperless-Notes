"""Restrained toasts for completed actions and automatic updates. Decisions never live in a toast."""

from __future__ import annotations

from PySide6.QtCore import QEasingCurve, QEvent, QObject, QTimer, QVariantAnimation
from PySide6.QtWidgets import QGraphicsOpacityEffect, QHBoxLayout, QLabel, QToolButton, QWidget

from paperless_notes.ui.shell.widgets import FloatingPanel
from paperless_notes.ui.theme.icons import glyph_icon
from paperless_notes.ui.theme.tokens import Theme

MAX_KEPT = 50


class Toast(FloatingPanel):
    """One message at a time, bottom centre of its host, faded in and hidden after the theme's toast
    duration."""

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
        self.close_button.clicked.connect(self.hide)
        layout.addWidget(self.close_button)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.timeout.connect(self.hide)
        self._fade: QVariantAnimation | None = None
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
        appearing = not self.isVisible()
        self.show()
        self.raise_()
        if appearing:
            self._fade_in()
        self._timer.start(self._theme.motion.toast_ms)

    def _fade_in(self) -> None:
        duration = self._theme.ms(self._theme.motion.normal_ms)
        if duration <= 0:
            return
        effect = QGraphicsOpacityEffect(self)
        effect.setOpacity(0.0)
        self.setGraphicsEffect(effect)
        fade = QVariantAnimation(self)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setDuration(duration)
        fade.setEasingCurve(QEasingCurve.Type.OutCubic)
        fade.valueChanged.connect(lambda value: effect.setOpacity(float(value)))
        fade.finished.connect(lambda: self.setGraphicsEffect(None))  # type: ignore[arg-type]
        self._fade = fade
        fade.start(QVariantAnimation.DeletionPolicy.DeleteWhenStopped)

    def _place(self) -> None:
        s = self._theme.spacing
        width = min(480, max(240, self._host.width() - 4 * s.xxl)) + 2 * self.SHADOW
        self.setFixedWidth(width)
        self.adjustSize()
        x = (self._host.width() - width) // 2
        y = self._host.height() - self.height() - s.xl - self._theme.metrics.status_bar
        self.move(max(0, x), max(0, y))

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt override
        if watched is self._host and event.type() == QEvent.Type.Resize and self.isVisible():
            self._place()
        return False
