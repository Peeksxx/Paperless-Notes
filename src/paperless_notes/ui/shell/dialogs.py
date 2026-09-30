"""Small dialogs: a name field that explains problems before anything is written, and confirmations.

UI code asks through a ``Prompter`` so tests can answer without a modal event loop.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from PySide6.QtWidgets import (
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

from paperless_notes.core.security.filenames import NameCheck


class NameDialog(QDialog):
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


class ConfirmDialog(QDialog):
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


class TableSizeDialog(QDialog):
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
