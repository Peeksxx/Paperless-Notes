"""Crash and error dialog with a shortcut to the log folder."""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QApplication, QMessageBox, QWidget

_showing = False


def show_error_dialog(title: str, message: str, logs_dir: Path, parent: QWidget | None = None) -> None:
    global _showing
    if _showing or QApplication.instance() is None:
        return
    _showing = True
    try:
        box = QMessageBox(parent or QApplication.activeWindow())
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle(title)
        box.setText(message)
        open_logs = box.addButton("Open log folder", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Close)
        box.exec()
        if box.clickedButton() is open_logs:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir)))
    finally:
        _showing = False
