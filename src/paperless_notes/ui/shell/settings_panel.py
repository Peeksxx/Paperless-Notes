"""Settings the app can honour, in four sections: appearance, writing, saving and the window. Each setting is
one row, its name on the left and its control on the right; on and off settings are switches that slide.
Settings read only at start say so under them."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QEnterEvent, QGuiApplication, QPainter, QPaintEvent, QPen
from PySide6.QtWidgets import (
    QAbstractButton,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core.settings import RANGES, Settings
from paperless_notes.ui.shell.dialogs import FadeInDialog
from paperless_notes.ui.shell.motion import Glow, Tween, mix
from paperless_notes.ui.shell.widgets import SectionLabel
from paperless_notes.ui.theme.tokens import LIGHT, Theme, theme

THEMES = (("system", "Follow Windows"), ("light", "Light"), ("dark", "Dark"))
ACCENTS = (("graphite", "Graphite"), ("lime", "Lime"))
FONTS = (("sans", "Sans (Segoe UI Variable)"), ("serif", "Serif (Georgia)"), ("mono", "Mono (Cascadia Mono)"))
RESTART = "Takes effect the next time Paperless Notes starts."
TRACK = QSize(34, 18)
SCREEN_SHARE = 0.72


def _combo(options: tuple[tuple[str, str], ...], value: str, name: str) -> QComboBox:
    combo = QComboBox()
    combo.setAccessibleName(name)
    for key, label in options:
        combo.addItem(label, key)
    index = combo.findData(value)
    combo.setCurrentIndex(max(0, index))
    return combo


def _spin(name: str, value: int, low: int, high: int, suffix: str, step: int = 1) -> QSpinBox:
    spin = QSpinBox()
    spin.setAccessibleName(name)
    spin.setRange(low, high)
    spin.setSingleStep(step)
    spin.setSuffix(suffix)
    spin.setValue(max(low, min(high, value)))
    return spin


class Switch(QAbstractButton):
    """An on and off switch. The knob slides across and the track fills with the accent colour; Space toggles
    it like a check box."""

    def __init__(self, name: str, theme: Theme, checked: bool = False) -> None:
        super().__init__()
        self._theme = theme
        self.setCheckable(True)
        self.setChecked(checked)
        self.setAccessibleName(name)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setFixedSize(TRACK.width() + 4, TRACK.height() + 4)
        self._knob = Tween(self, 1.0 if checked else 0.0, lambda _value: self.update())
        self._glow = Glow(self, lambda: self._theme.ms(self._theme.motion.fast_ms))
        self.toggled.connect(self._slide)

    @property
    def position(self) -> float:
        """Where the knob is, from 0 (off) to 1 (on)."""
        return float(self._knob.value)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(TRACK.width() + 4, TRACK.height() + 4)

    def _slide(self, on: bool) -> None:
        target = 1.0 if on else 0.0
        if self.isVisible():
            self._knob.to(target, self._theme.ms(self._theme.motion.normal_ms))
        else:
            self._knob.jump(target)

    def enterEvent(self, event: QEnterEvent) -> None:
        self._glow.set_on(True)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._glow.set_on(False)
        super().leaveEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        p = self._theme.palette
        on = self.position
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.isEnabled():
            painter.setOpacity(0.45)
        track = QRectF(2.5, 2.5, TRACK.width() - 1, TRACK.height() - 1)
        radius = track.height() / 2
        off_fill = mix(p.page, p.hover, self._glow.amount)
        on_fill = mix(p.accent, p.accent_hover, self._glow.amount)
        painter.setPen(QPen(mix(p.border_strong, on_fill, on), 1))
        painter.setBrush(mix(off_fill, on_fill, on))
        painter.drawRoundedRect(track, radius, radius)
        knob = track.height() - 6
        x = track.left() + 3 + (track.width() - knob - 6) * on
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(mix(p.text_secondary, p.on_accent, on))
        painter.drawEllipse(QRectF(x, track.top() + 3, knob, knob))
        if self.hasFocus() and self.window().testAttribute(Qt.WidgetAttribute.WA_KeyboardFocusChange):
            painter.setPen(QPen(QColor(p.focus), 1.5))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(track.adjusted(-2, -2, 2, 2), radius + 2, radius + 2)


class SettingsDialog(FadeInDialog):
    changed = Signal(object)

    def __init__(
        self, settings: Settings, parent: QWidget | None = None, current: Theme | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(560)
        self._settings = settings
        self._theme = current or theme(LIGHT)
        t = self._theme
        s = t.spacing
        self.theme = _combo(THEMES, settings.theme, "Theme")
        self.accent = _combo(ACCENTS, settings.accent, "Accent color")
        self.font_choice = _combo(FONTS, settings.note_font, "Editor font")
        self.width_spin = _spin("Reading width", settings.readable_width or 720, 480, 1600, " px", 20)
        self.full_width = self._switch("Use the full window width", settings.readable_width == 0)
        self.width_spin.setEnabled(settings.readable_width != 0)
        self.full_width.toggled.connect(lambda on: self.width_spin.setEnabled(not on))
        self.spelling = self._switch("Check spelling", settings.check_spelling)
        self.autocorrect = self._switch("Fix obvious typos as you type", settings.autocorrect)
        self.autocorrect.setEnabled(settings.check_spelling)
        self.spelling.toggled.connect(self.autocorrect.setEnabled)
        self.hints = self._switch("Show first-run tips", settings.show_hints)
        self.reset_hints = QPushButton("Show tips again")
        self.reset_hints.setProperty("kind", "quiet")
        self.reset_hints.setEnabled(bool(settings.dismissed_hints))
        self.reset_hints.clicked.connect(self._reset_hints)
        self._dismissed = settings.dismissed_hints
        low, high = RANGES["autosave_debounce_ms"]
        self.autosave = _spin("Autosave delay", settings.autosave_debounce_ms, low, high, " ms", 100)
        self.history = self._switch("Keep version history on this PC", settings.history_enabled)
        low, high = RANGES["history_max_mb"]
        self.history_mb = _spin("History size limit", settings.history_max_mb, low, high, " MB", 16)
        self.native_frame = self._switch("Use the native window frame", not settings.custom_frame)
        self.reduced_motion = self._switch("Reduce motion", settings.reduced_motion)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, s.xl)
        outer.setSpacing(s.md)
        body = QWidget()
        body.setObjectName("SettingsBody")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(s.xxl, s.xl, s.xxl, s.xs)
        layout.setSpacing(s.xs)
        self.body_scroll = QScrollArea()
        self.body_scroll.setObjectName("SettingsScroll")
        self.body_scroll.setWidgetResizable(True)
        self.body_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.body_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body_scroll.setWidget(body)
        self.body_scroll.viewport().setAutoFillBackground(False)
        outer.addWidget(self.body_scroll, 1)
        self._section(layout, "Appearance", first=True)
        self._row(
            layout, "Default theme", self.theme, "Notes with their own colours (Format, Theme) keep them."
        )
        self._row(layout, "Accent", self.accent)
        self._row(layout, "Editor font", self.font_choice)
        self._row(layout, "Reading width", self.width_spin)
        self._row(layout, "Use the full window width", self.full_width)
        self._section(layout, "Writing")
        self._row(layout, "Check spelling", self.spelling)
        self._row(
            layout,
            "Fix obvious typos as you type",
            self.autocorrect,
            "Names and less certain words are only underlined. Esc puts a corrected word back.",
        )
        self._row(layout, "Show first-run tips", self.hints, extra=self.reset_hints)
        self._section(layout, "Saving")
        self._row(layout, "Autosave delay", self.autosave, RESTART)
        self._row(layout, "Keep version history on this PC", self.history)
        self._row(layout, "History size limit", self.history_mb, RESTART)
        self._section(layout, "Window")
        self._row(layout, "Use the native window frame", self.native_frame, RESTART)
        self._row(
            layout, "Reduce motion", self.reduced_motion, "Panels, popups and the theme change at once."
        )
        layout.addStretch(1)
        buttons = QHBoxLayout()
        buttons.setContentsMargins(s.xxl, 0, s.xxl, 0)
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("Save settings")
        self.save_button.setProperty("kind", "primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        outer.addLayout(buttons)
        self._fit_height(body)

    def _fit_height(self, body: QWidget) -> None:
        """As tall as the settings need, but no more than most of the screen; the rest scrolls."""
        screen = self.screen() or QGuiApplication.primaryScreen()
        room = round(screen.availableGeometry().height() * SCREEN_SHARE) if screen is not None else 600
        wanted = body.sizeHint().height() + self.save_button.sizeHint().height() + 3 * self._theme.spacing.xl
        self.resize(max(self.minimumWidth(), 600), max(320, min(wanted, room)))

    def _switch(self, name: str, checked: bool) -> Switch:
        return Switch(name, self._theme, checked)

    def _section(self, layout: QVBoxLayout, title: str, first: bool = False) -> None:
        if not first:
            layout.addSpacing(self._theme.spacing.lg)
        layout.addWidget(SectionLabel(title, self._theme))

    def _row(
        self, layout: QVBoxLayout, title: str, control: QWidget, note: str = "", extra: QWidget | None = None
    ) -> None:
        row = QGridLayout()
        row.setContentsMargins(0, self._theme.spacing.xxs, 0, self._theme.spacing.xxs)
        row.setHorizontalSpacing(self._theme.spacing.md)
        row.setVerticalSpacing(0)
        label = QLabel(title)
        label.setBuddy(control)
        row.addWidget(label, 0, 0, Qt.AlignmentFlag.AlignVCenter)
        row.setColumnStretch(0, 1)
        if extra is not None:
            row.addWidget(extra, 0, 1, Qt.AlignmentFlag.AlignVCenter)
        row.addWidget(control, 0, 2, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight)
        if isinstance(control, QComboBox | QSpinBox):
            control.setMinimumWidth(220)
        if note:
            hint = QLabel(note)
            hint.setProperty("role", "muted")
            hint.setWordWrap(True)
            row.addWidget(hint, 1, 0, 1, 3)
        layout.addLayout(row)

    def _reset_hints(self) -> None:
        self._dismissed = ()
        self.hints.setChecked(True)
        self.reset_hints.setEnabled(False)

    def result_settings(self) -> Settings:
        return replace(
            self._settings,
            theme=str(self.theme.currentData()),
            accent=str(self.accent.currentData()),
            note_font=str(self.font_choice.currentData()),
            readable_width=0 if self.full_width.isChecked() else self.width_spin.value(),
            autosave_debounce_ms=self.autosave.value(),
            history_enabled=self.history.isChecked(),
            history_max_mb=self.history_mb.value(),
            custom_frame=not self.native_frame.isChecked(),
            reduced_motion=self.reduced_motion.isChecked(),
            check_spelling=self.spelling.isChecked(),
            autocorrect=self.autocorrect.isChecked(),
            show_hints=self.hints.isChecked(),
            dismissed_hints=self._dismissed,
        )

    def _save(self) -> None:
        self.changed.emit(self.result_settings())
        self.accept()
