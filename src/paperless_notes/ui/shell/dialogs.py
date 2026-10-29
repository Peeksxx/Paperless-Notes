"""Small dialogs: a name field that explains problems before anything is written, confirmations, and a
note's own colours.

UI code asks through a ``Prompter`` so tests can answer without a modal event loop.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from PySide6.QtCore import Signal
from PySide6.QtGui import QColor, QShowEvent
from PySide6.QtWidgets import (
    QColorDialog,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core.local_state import NoteColors
from paperless_notes.core.security.filenames import NameCheck
from paperless_notes.ui.shell.motion import fade_in_window
from paperless_notes.ui.theme.tokens import Theme, contrast

READABLE_CONTRAST = 4.5
PREVIEW_TEXT = "The quick brown fox jumps over the lazy dog."


def window_motion_ms(widget: QWidget) -> int:
    """How long a dialog of ``widget``'s window takes to fade in: the main window's normal duration, or 0 with
    Reduce motion or when the dialog has no themed window behind it."""
    parent = widget.parentWidget()
    current = getattr(parent.window(), "theme", None) if parent is not None else None
    return current.ms(current.motion.normal_ms) if isinstance(current, Theme) else 0


class FadeInDialog(QDialog):
    """A dialog that fades in each time it opens."""

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not event.spontaneous():
            fade_in_window(self, window_motion_ms(self))


class NameDialog(FadeInDialog):
    """Asks for a name and validates it live; OK stays disabled while the name has a problem."""

    def __init__(
        self,
        title: str,
        prompt: str,
        validate: Callable[[str], NameCheck],
        initial: str = "",
        action: str = "Create",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        self._validate = validate
        layout = QVBoxLayout(self)
        label = QLabel(prompt)
        label.setWordWrap(True)
        self.field = QLineEdit(initial)
        self.field.setAccessibleName(prompt)
        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.setProperty("role", "secondary")
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.accept_button = QPushButton(action)
        self.accept_button.setProperty("kind", "primary")
        self.accept_button.setDefault(True)
        self.buttons.addButton(self.accept_button, QDialogButtonBox.ButtonRole.AcceptRole)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(label)
        layout.addWidget(self.field)
        layout.addWidget(self.problem)
        layout.addWidget(self.buttons)
        self.field.textChanged.connect(self._check)
        self.field.selectAll()
        self._check(initial)

    def _check(self, text: str) -> None:
        check = self._validate(text)
        self.accept_button.setEnabled(check.ok)
        self.problem.setText("" if check.ok else check.problem or "")
        self.problem.setVisible(not check.ok and bool(text.strip()))

    def value(self) -> str:
        return self.field.text()


class ConfirmDialog(FadeInDialog):
    """A decision in context: a plain explanation and one clearly worded action button."""

    def __init__(
        self, title: str, text: str, action: str, danger: bool = False, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        body = QLabel(text)
        body.setWordWrap(True)
        layout.addWidget(body)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.action_button = QPushButton(action)
        self.action_button.setProperty("kind", "danger" if danger else "primary")
        buttons.addButton(self.action_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        cancel = buttons.button(QDialogButtonBox.StandardButton.Cancel)
        if cancel is not None:
            cancel.setDefault(True)
        layout.addWidget(buttons)


@dataclass(frozen=True, slots=True)
class ColorChoice:
    """What the note theme dialog decided: the note's own (background, text) colours, or None to follow the
    default theme again."""

    colors: NoteColors | None


class ColorSwatch(QPushButton):
    """A button showing one colour and its code; clicking it opens the colour picker."""

    changed = Signal(str)

    def __init__(self, label: str, color: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._label = label
        self._color = ""
        self.setToolTip(f"Choose the {label.lower()} colour")
        self.clicked.connect(self._pick)
        self.set_color(color)

    def color(self) -> str:
        return self._color

    def set_color(self, color: str) -> None:
        self._color = QColor(color).name()
        ink = "#000000" if contrast("#000000", self._color) >= contrast("#ffffff", self._color) else "#ffffff"
        self.setText(self._color)
        self.setAccessibleName(f"{self._label} colour, {self._color}")
        self.setStyleSheet(f"QPushButton {{ background: {self._color}; color: {ink}; }}")
        self.changed.emit(self._color)

    def _pick(self) -> None:
        chosen = QColorDialog.getColor(QColor(self._color), self, f"{self._label} colour")
        if chosen.isValid():
            self.set_color(chosen.name())


class NoteThemeDialog(FadeInDialog):
    """One note's background and text colours, chosen with the colour picker, with a preview and a warning
    when the text would be hard to read. "Use default theme" goes back to the theme set in Settings."""

    def __init__(self, start: NoteColors, custom: bool, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Note theme")
        self.setMinimumWidth(420)
        self._reset = False
        layout = QVBoxLayout(self)
        intro = QLabel("Colours for this note on this PC. Other notes keep the default theme from Settings.")
        intro.setWordWrap(True)
        form = QFormLayout()
        self.background = ColorSwatch("Background", start[0])
        self.text = ColorSwatch("Text", start[1])
        form.addRow("Background", self.background)
        form.addRow("Text", self.text)
        self.preview = QLabel(PREVIEW_TEXT)
        self.preview.setWordWrap(True)
        self.preview.setMargin(12)
        self.preview.setAccessibleName("Preview")
        self.warning = QLabel("")
        self.warning.setWordWrap(True)
        self.warning.setProperty("role", "secondary")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.default_button = QPushButton("Use default theme")
        self.default_button.setEnabled(custom)
        self.default_button.clicked.connect(self._use_default)
        buttons.addButton(self.default_button, QDialogButtonBox.ButtonRole.ResetRole)
        self.save_button = QPushButton("Save")
        self.save_button.setProperty("kind", "primary")
        self.save_button.setDefault(True)
        buttons.addButton(self.save_button, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.preview)
        layout.addWidget(self.warning)
        layout.addWidget(buttons)
        self.background.changed.connect(self._update)
        self.text.changed.connect(self._update)
        self._update()

    def _use_default(self) -> None:
        self._reset = True
        self.accept()

    def _update(self, _color: str = "") -> None:
        background, text = self.background.color(), self.text.color()
        self.preview.setStyleSheet(f"QLabel {{ background: {background}; color: {text}; }}")
        ratio = contrast(text, background)
        advice = " The text may be hard to read; 4.5 to 1 or more is easier."
        self.warning.setText(f"Contrast {ratio:.1f} to 1.{advice if ratio < READABLE_CONTRAST else ''}")

    def choice(self) -> ColorChoice:
        return ColorChoice(None if self._reset else (self.background.color(), self.text.color()))


class Prompter(Protocol):
    def confirm(self, title: str, text: str, action: str, danger: bool = False) -> bool: ...

    def ask_name(
        self,
        title: str,
        prompt: str,
        validate: Callable[[str], NameCheck],
        initial: str = "",
        action: str = "Create",
    ) -> str | None: ...

    def choose_folder(self, title: str, start: str) -> str | None: ...

    def choose_file(self, title: str, start: str) -> str | None: ...

    def choose_image(self, title: str, start: str) -> str | None: ...

    def ask_table_size(self) -> tuple[int, int] | None: ...

    def choose_save_file(self, title: str, start: str, filter_text: str) -> str | None: ...

    def ask_note_colors(self, start: NoteColors, custom: bool) -> ColorChoice | None: ...


class DialogPrompter:
    """The real prompter: modal dialogs parented to the main window."""

    def __init__(self, parent: QWidget) -> None:
        self._parent = parent

    def confirm(self, title: str, text: str, action: str, danger: bool = False) -> bool:
        return ConfirmDialog(title, text, action, danger, self._parent).exec() == QDialog.DialogCode.Accepted

    def ask_name(
        self,
        title: str,
        prompt: str,
        validate: Callable[[str], NameCheck],
        initial: str = "",
        action: str = "Create",
    ) -> str | None:
        dialog = NameDialog(title, prompt, validate, initial, action, self._parent)
        return dialog.value() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def choose_folder(self, title: str, start: str) -> str | None:
        return QFileDialog.getExistingDirectory(self._parent, title, start) or None

    def choose_file(self, title: str, start: str) -> str | None:
        path, _ = QFileDialog.getOpenFileName(
            self._parent,
            title,
            start,
            "Markdown (*.md *.markdown);;Text (*.txt);;All notes (*.md *.markdown *.txt)",
        )
        return path or None

    def choose_image(self, title: str, start: str) -> str | None:
        path, _ = QFileDialog.getOpenFileName(
            self._parent, title, start, "Images (*.png *.jpg *.jpeg *.gif *.webp *.bmp)"
        )
        return path or None

    def ask_table_size(self) -> tuple[int, int] | None:
        dialog = TableSizeDialog(self._parent)
        return dialog.size_chosen() if dialog.exec() == QDialog.DialogCode.Accepted else None

    def choose_save_file(self, title: str, start: str, filter_text: str) -> str | None:
        """A save dialog; replacing an existing file is confirmed by the caller, not here."""
        path, _ = QFileDialog.getSaveFileName(
            self._parent, title, start, filter_text, "", QFileDialog.Option.DontConfirmOverwrite
        )
        return path or None

    def ask_note_colors(self, start: NoteColors, custom: bool) -> ColorChoice | None:
        """The note theme dialog; None when cancelled."""
        dialog = NoteThemeDialog(start, custom, self._parent)
        return dialog.choice() if dialog.exec() == QDialog.DialogCode.Accepted else None


class TableSizeDialog(FadeInDialog):
    """Columns and body rows for a new table, bounded, with a safe default of 3 by 2."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Insert table")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.columns = QSpinBox()
        self.columns.setRange(1, 20)
        self.columns.setValue(3)
        self.columns.setAccessibleName("Columns")
        self.rows = QSpinBox()
        self.rows.setRange(1, 200)
        self.rows.setValue(2)
        self.rows.setAccessibleName("Rows below the header")
        form.addRow("Columns", self.columns)
        form.addRow("Rows below the header", self.rows)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        insert = QPushButton("Insert table")
        insert.setProperty("kind", "primary")
        insert.setDefault(True)
        buttons.addButton(insert, QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def size_chosen(self) -> tuple[int, int]:
        return self.columns.value(), self.rows.value()
