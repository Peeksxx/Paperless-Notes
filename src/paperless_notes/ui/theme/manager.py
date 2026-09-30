"""Applies the theme to the application and follows the Windows color scheme when asked to."""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QFont, QGuiApplication, QStyleHints
from PySide6.QtWidgets import QApplication

from paperless_notes.ui.theme.qss import build_palette, build_stylesheet
from paperless_notes.ui.theme.tokens import ACCENTS, DARK, GRAPHITE, LIGHT, MODES, SYSTEM, Theme, theme


def ui_font(current: Theme) -> QFont:
    t = current.typography
    font = QFont()
    font.setFamilies(list(t.ui_families))
    font.setPointSizeF(t.ui_pt)
    tag = QFont.Tag.fromString("opsz")
    if tag is not None:
        font.setVariableAxis(tag, t.text_opsz)
    return font


class ThemeManager(QObject):
    changed = Signal(object)

    def __init__(
        self,
        app: QApplication,
        mode: str = SYSTEM,
        accent: str = GRAPHITE,
        reduced_motion: bool = False,
        hints: QStyleHints | None = None,
    ) -> None:
        super().__init__()
        self._app = app
        self._hints = hints if hints is not None else QGuiApplication.styleHints()
        self._mode = mode if mode in MODES else SYSTEM
        self._accent = accent if accent in ACCENTS else GRAPHITE
        self._reduced = reduced_motion
        self._theme = theme(self.resolved_mode(), self._accent, self._reduced)
        self._hints.colorSchemeChanged.connect(self._on_scheme_changed)

    @property
    def theme(self) -> Theme:
        return self._theme

    @property
    def mode(self) -> str:
        return self._mode

    def resolved_mode(self) -> str:
        if self._mode == SYSTEM:
            return DARK if self._hints.colorScheme() == Qt.ColorScheme.Dark else LIGHT
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode in MODES and mode != self._mode:
            self._mode = mode
            self.apply()

    def set_accent(self, accent: str) -> None:
        if accent in ACCENTS and accent != self._accent:
            self._accent = accent
            self.apply()

    def set_reduced_motion(self, reduced: bool) -> None:
        if reduced != self._reduced:
            self._reduced = reduced
            self.apply()

    def apply(self) -> None:
        self._theme = theme(self.resolved_mode(), self._accent, self._reduced)
        self._app.setFont(ui_font(self._theme))
        self._app.setPalette(build_palette(self._theme))
        self._app.setStyleSheet(build_stylesheet(self._theme))
        self.changed.emit(self._theme)

    def _on_scheme_changed(self, _scheme: Qt.ColorScheme) -> None:
        if self._mode == SYSTEM:
            self.apply()
