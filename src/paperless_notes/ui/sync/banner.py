"""A non-modal banner for one diagnosis. It offers exactly the engine's listed actions, nothing more.

``default_action`` only decides emphasis: Compare is the primary button when, and only when, it is the
stated default. Dismiss appears only when the engine lists it. Nothing is applied without a click.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from paperless_notes.core.oracle import Action, Diagnosis, Severity
from paperless_notes.ui.shell.flow import FlowLayout
from paperless_notes.ui.theme.icons import glyph_icon
from paperless_notes.ui.theme.tokens import Theme

ACTION_LABELS: dict[Action, tuple[str, str]] = {
    Action.SHOW_DIFF: ("Compare versions", "Show exactly what differs between the versions"),
    Action.KEEP_MINE: ("Keep my version", "Put your version back into the note and save it normally"),
    Action.TAKE_THEIRS: (
        "Keep the version on disk",
        "Keep the file as it is now; your text stays in history",
    ),
    Action.KEEP_BOTH: ("Keep both", "Save your version as a separate copy next to the note"),
    Action.RESTORE_VERSION: (
        "Restore previous version",
        "Put the previous version back and save it normally",
    ),
    Action.RESTORE_FILE: ("Restore the file", "Write your text back to the original location"),
    Action.RETRY_NOW: ("Try again now", "Check the file and try saving again right away"),
    Action.DISMISS: ("Dismiss", "Hide this message; it will not come back for this change"),
}
SEVERITY_NAMES = {
    Severity.ERROR: "error",
    Severity.WARN: "warn",
    Severity.NOTICE: "notice",
    Severity.INFO: "info",
}
_ICONS = {"error": "error", "warn": "warning", "notice": "info", "info": "info"}


def shows_banner(diagnosis: Diagnosis | None) -> bool:
    return diagnosis is not None and diagnosis.severity >= Severity.NOTICE


class DiagnosisBanner(QFrame):
    action_triggered = Signal(object, object)

    def __init__(self, diagnosis: Diagnosis, theme: Theme, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("Banner")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.diagnosis = diagnosis
        severity = SEVERITY_NAMES[diagnosis.severity]
        self.setProperty("severity", severity)
        self.setAccessibleName(f"{diagnosis.title}. {diagnosis.message}")
        outer = QHBoxLayout(self)
        s = theme.spacing
        outer.setContentsMargins(s.lg, s.md, s.lg, s.md)
        outer.setSpacing(s.md)
        p = theme.palette
        color = {"error": p.error_text, "warn": p.warning_text}.get(severity, p.text_secondary)
        icon = QLabel()
        icon.setPixmap(glyph_icon(_ICONS[severity], color).pixmap(18, 18))
        icon.setAlignment(Qt.AlignmentFlag.AlignTop)
        outer.addWidget(icon)
        body = QVBoxLayout()
        body.setSpacing(s.xs)
        self.title = QLabel(diagnosis.title)
        self.title.setObjectName("BannerTitle")
        self.title.setWordWrap(True)
        self.message = QLabel(diagnosis.message)
        self.message.setWordWrap(True)
        self.message.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.result = QLabel("")
        self.result.setWordWrap(True)
        self.result.hide()
        body.addWidget(self.title)
        body.addWidget(self.message)
        body.addWidget(self.result)
        self.buttons: dict[Action, QPushButton] = {}
        row = FlowLayout(spacing=s.sm)
        for action in diagnosis.actions:
            label, tip = ACTION_LABELS[action]
            button = QPushButton(label)
            button.setToolTip(tip)
            primary = action is Action.SHOW_DIFF and diagnosis.default_action is Action.SHOW_DIFF
            button.setProperty(
                "kind", "primary" if primary else ("quiet" if action is Action.DISMISS else "")
            )
            button.clicked.connect(lambda _c=False, a=action: self.action_triggered.emit(self.diagnosis, a))
            self.buttons[action] = button
            row.addWidget(button)
        body.addLayout(row)
        outer.addLayout(body, 1)

    def show_result(self, text: str) -> None:
        """Explain a refused action in place; the diagnosis stays until it is really handled."""
        self.result.setText(text)
        self.result.setVisible(bool(text))
