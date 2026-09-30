"""The outline panel beside the page: the headings of the active note. Search and tags live in the search
palette (Ctrl+Shift+P, then # for tags)."""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QFrame, QLabel, QListWidget, QListWidgetItem, QToolButton, QVBoxLayout, QWidget

from paperless_notes.mdio.outline import Outline
from paperless_notes.ui.shell.widgets import SectionLabel
from paperless_notes.ui.theme.icons import glyph_icon
from paperless_notes.ui.theme.tokens import Theme


class OutlinePage(QWidget):
    """The headings of the active note; Enter or a click reveals one in the active view."""

    heading_chosen = Signal(int)

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(theme.spacing.sm)
        self.list = QListWidget()
        self.list.setProperty("role", "plain")
        self.list.setAccessibleName("Outline of this note")
        self.list.itemActivated.connect(self._choose)
        self.list.itemClicked.connect(self._choose)
        self.state = QLabel("Open a note to see its headings.")
        self.state.setProperty("role", "muted")
        self.state.setWordWrap(True)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.state)

    def show_outline(self, outline: Outline | None) -> None:
        current = self.list.currentRow()
        self.list.clear()
        if outline is None:
            self.state.setText("Open a note to see its headings.")
        elif outline.too_large:
            self.state.setText("This note is too large for an outline.")
        elif not outline.headings:
            self.state.setText("No headings in this note. Lines starting with # become headings.")
        else:
            self.state.setText("Showing the first headings only." if outline.truncated else "")
        for heading in outline.headings if outline is not None else ():
            text = heading.text or "(empty heading)"
            item = QListWidgetItem("    " * (heading.level - 1) + text)
            item.setData(Qt.ItemDataRole.UserRole, heading.line)
            item.setToolTip(f"Heading {heading.level}, line {heading.line + 1}")
            item.setData(Qt.ItemDataRole.AccessibleTextRole, f"Heading {heading.level}: {text}")
            self.list.addItem(item)
        if 0 <= current < self.list.count():
            self.list.setCurrentRow(current)
        self.state.setVisible(bool(self.state.text()))

    def _choose(self, item: QListWidgetItem) -> None:
        self.heading_chosen.emit(int(item.data(Qt.ItemDataRole.UserRole)))


class ToolPanel(QFrame):
    """The outline behind a titled header with a close button."""

    closed = Signal()
    PAGES = ("outline",)

    def __init__(self, theme: Theme, outline: OutlinePage) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(232)
        self.setMaximumWidth(300)
        s = theme.spacing
        layout = QVBoxLayout(self)
        layout.setContentsMargins(s.lg, s.md, s.md, s.lg)
        layout.setSpacing(s.sm)
        self.close_button = QToolButton()
        self.close_button.setAccessibleName("Close outline")
        self.close_button.setToolTip("Close the outline (Ctrl+Shift+O)")
        self.close_button.clicked.connect(self.close_panel)
        self.heading = SectionLabel("Outline", theme, (self.close_button,))
        layout.addWidget(self.heading)
        self.outline = outline
        layout.addWidget(outline, 1)
        self.apply_theme(theme)
        self.hide()

    def current(self) -> str:
        return "outline" if self.isVisible() else ""

    def show_page(self, _name: str = "outline") -> None:
        self.show()
        self.outline.list.setFocus(Qt.FocusReason.ShortcutFocusReason)

    def toggle(self) -> None:
        if self.isVisible():
            self.close_panel()
        else:
            self.show_page()

    def close_panel(self) -> None:
        self.hide()
        self.closed.emit()

    def apply_theme(self, theme: Theme) -> None:
        p = theme.palette
        self.heading.apply_theme(theme)
        self.close_button.setIcon(glyph_icon("close", p.text_secondary, p.text))
