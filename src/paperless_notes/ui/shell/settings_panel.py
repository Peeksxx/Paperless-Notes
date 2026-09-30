"""Settings the app can honour: theme, accent, editor font, reading width, autosave, history, window frame,
reduced motion and first-run hints. Settings read only at start say so."""

from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core.settings import RANGES, Settings

THEMES = (("system", "Follow Windows"), ("light", "Light"), ("dark", "Dark"))
ACCENTS = (("graphite", "Graphite"), ("lime", "Lime"))
FONTS = (("sans", "Sans (Segoe UI Variable)"), ("serif", "Serif (Georgia)"), ("mono", "Mono (Cascadia Mono)"))
RESTART = "Takes effect the next time Paperless Notes starts."


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


class SettingsDialog(QDialog):
    changed = Signal(object)

    def __init__(self, settings: Settings, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(520)
        self._settings = settings
        layout = QVBoxLayout(self)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.theme = _combo(THEMES, settings.theme, "Theme")
        self.accent = _combo(ACCENTS, settings.accent, "Accent color")
        self.font_choice = _combo(FONTS, settings.note_font, "Editor font")
        self.full_width = QCheckBox("Use the full window width")
        self.full_width.setChecked(settings.readable_width == 0)
        self.width_spin = _spin("Reading width", settings.readable_width or 720, 480, 1600, " px", 20)
        self.width_spin.setEnabled(settings.readable_width != 0)
        self.full_width.toggled.connect(lambda on: self.width_spin.setEnabled(not on))
        low, high = RANGES["autosave_debounce_ms"]
        self.autosave = _spin("Autosave delay", settings.autosave_debounce_ms, low, high, " ms", 100)
        self.history = QCheckBox("Keep version history on this PC")
        self.history.setChecked(settings.history_enabled)
        low, high = RANGES["history_max_mb"]
        self.history_mb = _spin("History size limit", settings.history_max_mb, low, high, " MB", 16)
        self.native_frame = QCheckBox("Use the native window frame")
        self.native_frame.setChecked(not settings.custom_frame)
        self.reduced_motion = QCheckBox("Reduce motion")
        self.reduced_motion.setChecked(settings.reduced_motion)
        self.hints = QCheckBox("Show first-run tips")
        self.hints.setChecked(settings.show_hints)
        self.reset_hints = QPushButton("Show tips again")
        self.reset_hints.setProperty("kind", "quiet")
        self.reset_hints.setEnabled(bool(settings.dismissed_hints))
        self.reset_hints.clicked.connect(self._reset_hints)
        self._dismissed = settings.dismissed_hints
        form.addRow("Theme", self.theme)
        form.addRow("Accent", self.accent)
        form.addRow("Editor font", self.font_choice)
        form.addRow("Reading width", self.width_spin)
        form.addRow("", self.full_width)
        form.addRow("Autosave delay", self.autosave)
        form.addRow("", self._note(RESTART))
        form.addRow("History", self.history)
        form.addRow("History size limit", self.history_mb)
        form.addRow("", self._note(RESTART))
        form.addRow("Window", self.native_frame)
        form.addRow("", self._note(RESTART))
        form.addRow("Motion", self.reduced_motion)
        hints_row = QHBoxLayout()
        hints_row.addWidget(self.hints)
        hints_row.addWidget(self.reset_hints)
        hints_row.addStretch(1)
        form.addRow("Tips", hints_row)
        layout.addLayout(form)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.save_button = QPushButton("Save settings")
        self.save_button.setProperty("kind", "primary")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        buttons.addWidget(cancel)
        buttons.addWidget(self.save_button)
        layout.addLayout(buttons)

    @staticmethod
    def _note(text: str) -> QLabel:
        label = QLabel(text)
        label.setProperty("role", "muted")
        label.setWordWrap(True)
        return label

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
            show_hints=self.hints.isChecked(),
            dismissed_hints=self._dismissed,
        )

    def _save(self) -> None:
        self.changed.emit(self.result_settings())
        self.accept()
