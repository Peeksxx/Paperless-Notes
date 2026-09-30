"""The Help panel (F1, the ? button, or "help" in the palette) and the three first-run hints."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.ui.shell.flow import FlowLayout
from paperless_notes.ui.shell.widgets import FloatingPanel
from paperless_notes.ui.theme.icons import glyph_icon
from paperless_notes.ui.theme.tokens import Theme

SECTIONS: tuple[tuple[str, str], ...] = (
    (
        "Search",
        "Ctrl+Shift+P opens the search palette; so does the search box at the top of the window. Type to "
        "find notes by name or by the words inside them, then press Enter to open one with the match "
        "selected. Start with > to run any command by name, # to list tags and the notes that carry them, "
        "@ to jump to a heading in the note you are in, : to go to a line, and ? to see these prefixes. The "
        "prefixes are also listed along the bottom of the palette, and each one can be clicked. "
        "Ctrl+Shift+F opens the palette for notes and Ctrl+G for a line number. The search index is kept on "
        "this PC only. Notes that are too large, not text, or only in OneDrive are listed by name and not "
        "searched.",
    ),
    (
        "Home",
        "Home appears when no note is open, and from the Home button in the sidebar or the logo in the top "
        "bar (Alt+Home). It shows notes to continue, the notes that changed most recently in your library on "
        "any PC, pinned notes, your most used tags and your library folders. Open tabs stay open while Home "
        "is shown; choose a tab to return to it.",
    ),
    (
        "Files",
        "Every note is an ordinary Markdown or text file on this PC. The library on the left shows folders "
        "you add with the Add folder button next to Library. Hover a folder and click + to create a note in "
        "it. Rename, Move and Delete are in each item's right-click menu; Delete moves the item to the "
        "Recycle Bin after you confirm. A dot next to a note means it is open; amber means it has unsaved "
        "changes and red means it needs your attention.",
    ),
    (
        "Tabs",
        "Each open note has a tab. The + after the last tab creates a note. Drag tabs to reorder them, "
        "middle-click to close one, and right-click to pin, copy the path, close others or reopen a closed "
        "tab. A dot on a tab means unsaved changes (amber) or a problem (red). The arrow at the right lists "
        "every open tab.",
    ),
    (
        "Saving",
        "Notes save automatically a moment after you stop typing, when you switch tabs and when the window "
        "loses focus. Ctrl+S saves right away. The file keeps its encoding and line endings, and a note you "
        "only read is never rewritten. If a save fails, the text stays in the window and in a recovery "
        "journal, and the line under the title says so. The bottom of the sidebar summarizes every open "
        "note.",
    ),
    (
        "OneDrive status",
        "The line under the title says where the note is saved: on this PC, waiting to upload, or uploaded. "
        "Uploaded is what OneDrive reports; it is not a check of the copy in the cloud. When the status is "
        "not known, the line stays empty rather than guessing.",
    ),
    (
        "Changes from elsewhere",
        "When the file changes on another device or in another app, a note you have not edited is updated "
        "and the changed lines get a mark in the margin. Hover a mark, or use Show next change, to see what "
        "was there before. Clear marks hides them; the text is not changed.",
    ),
    (
        "Conflicts",
        "If you and someone else changed the same lines, nothing is overwritten. A message under the title "
        "offers exactly the choices that are safe for that situation. Compare opens Base, Yours and Theirs "
        "side by side with an editable result. Keep both saves your version as a separate copy next to "
        "the note.",
    ),
    (
        "History",
        "Version history (Ctrl+Shift+H) keeps earlier versions of each note on this PC. Move the slider to "
        "compare any version with your text; Restore puts it back after you confirm, and keeps the current "
        "text in history first. Clear history deletes the kept versions and the saved sync comparisons for "
        "that note.",
    ),
    (
        "Writing",
        "The Insert and Format buttons above the text hold every writing command: headings, lists, task "
        "lists, quotes, code blocks, tables, images, links, bold, italic, strikethrough, inline code and "
        "moving or duplicating lines. Typing / at the start of a line or after a space opens the same "
        "Insert commands at the caret; keep typing to filter, Enter applies, Escape closes. Selecting text "
        "shows a small bar for formatting (Alt+F10 moves to it). Enter continues a list, Enter on an empty "
        "item ends it, and Tab or Shift+Tab indents list items. In a table, Tab moves to the next cell and "
        "the Table button adds or removes rows and columns. Pasted or dropped images are copied into an "
        "assets folder next to the note; undo removes the reference but keeps the file. Ctrl+Shift+V "
        "pastes plain text. Each command is one undo step.",
    ),
    (
        "Tags and outline",
        "A tag is # followed by letters or digits, such as #project or #to-read; tags in code, links and "
        "HTML do not count. Type # in the palette to list every tag with its number of notes. The outline "
        "panel (Ctrl+Shift+O) lists the headings of the note you are in; @ in the palette jumps to one.",
    ),
    (
        "Find, split view and export",
        "The Note menu above the text holds Find in note (Ctrl+F), Replace in note (Ctrl+H), Outline, Split "
        "view, Export as HTML, PDF or plain text, Copy as Markdown and Copy as rich text. In the find bar, "
        "Enter and F3 go to the next match, Shift+Enter and Shift+F3 to the previous one, and Escape closes "
        "it; Replace all is one undo step. Split view shows a second note, or the same note, side by side; "
        "both views of one note share its text and saving, and F6 moves between the panes. Exports never "
        "change the note, never load anything from the internet, and include only images from the note's "
        "assets folder.",
    ),
    (
        "Keyboard",
        "Ctrl+Shift+P Search (> commands, # tags, @ headings, : line). Ctrl+N New note. Ctrl+O Open file. "
        "Ctrl+S Save now. Ctrl+W Close tab. Ctrl+Shift+T Reopen closed tab. Ctrl+Tab Recent tab. Alt+Left "
        "and Alt+Right Back and forward. Alt+Home Home. Ctrl+\\ Sidebar. Ctrl+Plus, Ctrl+Minus and Ctrl+0 "
        "Zoom. Ctrl+click Open a link. F1 Help. Ctrl+B Bold. Ctrl+I Italic. Ctrl+Shift+X Strikethrough. "
        "Ctrl+E Inline code. Ctrl+K Link. Ctrl+Alt+1 to 3 Headings. Alt+Up and Alt+Down Move lines. "
        "Shift+Alt+Down Duplicate lines. Ctrl+Shift+F Search notes. Ctrl+G Go to line. Ctrl+Shift+O Outline. "
        "Ctrl+F Find. Ctrl+H Replace. F3 and Shift+F3 Next and previous match. F6 Other pane. Every shortcut "
        "also has a button, a menu item or a palette command.",
    ),
)


def help_html() -> str:
    parts = [f"<h3>{title}</h3><p>{text}</p>" for title, text in SECTIONS]
    return "".join(parts)


class HelpPanel(QFrame):
    closed = Signal()

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.setObjectName("Panel")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setMinimumWidth(280)
        self.setMaximumWidth(420)
        layout = QVBoxLayout(self)
        s = theme.spacing
        layout.setContentsMargins(s.xl, s.lg, s.md, s.lg)
        head = QHBoxLayout()
        title = QLabel("Help")
        title.setProperty("role", "heading")
        head.addWidget(title)
        head.addStretch(1)
        self.close_button = QToolButton()
        self.close_button.setAccessibleName("Close help")
        self.close_button.setToolTip("Close help (F1)")
        self.close_button.clicked.connect(self.close_panel)
        head.addWidget(self.close_button)
        layout.addLayout(head)
        self.browser = QTextBrowser()
        self.browser.setOpenLinks(False)
        self.browser.setOpenExternalLinks(False)
        self.browser.setProperty("role", "plain")
        self.browser.setAccessibleName("Help text")
        self.browser.setHtml(help_html())
        layout.addWidget(self.browser, 1)
        self.apply_theme(theme)
        self.hide()

    def apply_theme(self, theme: Theme) -> None:
        p = theme.palette
        self.close_button.setIcon(glyph_icon("close", p.text_secondary, p.text))

    def close_panel(self) -> None:
        self.hide()
        self.closed.emit()

    def toggle(self) -> None:
        if self.isVisible():
            self.close_panel()
        else:
            self.show()
            self.browser.setFocus()


@dataclass(frozen=True, slots=True)
class Hint:
    id: str
    title: str
    text: str


HINTS: tuple[Hint, ...] = (
    Hint(
        "sidebar",
        "Your notes",
        "Library folders are on the left. New note creates a note; the buttons next to Library add a folder "
        "or create one.",
    ),
    Hint(
        "search",
        "One search for everything",
        "Press Ctrl+Shift+P, or click here, to find notes by name or text. Start with > for commands, # for "
        "tags, @ for headings.",
    ),
    Hint(
        "sync",
        "Saving and history",
        "The line under each title says where the note is saved. Version history keeps earlier versions "
        "here.",
    ),
)


def pending_hints(show: bool, dismissed: tuple[str, ...]) -> list[Hint]:
    return [h for h in HINTS if h.id not in dismissed] if show else []


class HintBubble(FloatingPanel):
    """A small, dismissible explanation placed next to the control it describes."""

    dismissed = Signal(str)
    hide_all = Signal()

    def __init__(self, host: QWidget, theme: Theme) -> None:
        super().__init__(host, theme)
        self._host = host
        self.hint: Hint | None = None
        s = theme.spacing
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.inner_margins(s.lg, s.md))
        layout.setSpacing(s.xs)
        self.title = QLabel("")
        self.title.setObjectName("BannerTitle")
        self.title.setProperty("role", "heading")
        self.text = QLabel("")
        self.text.setWordWrap(True)
        self.text.setProperty("role", "secondary")
        layout.addWidget(self.title)
        layout.addWidget(self.text)
        row = FlowLayout(spacing=s.sm)
        self.hide_all_button = QPushButton("Hide all tips")
        self.hide_all_button.setProperty("kind", "quiet")
        self.hide_all_button.clicked.connect(self.hide_all.emit)
        self.ok_button = QPushButton("Got it")
        self.ok_button.setProperty("kind", "primary")
        self.ok_button.clicked.connect(self._dismiss)
        row.addWidget(self.hide_all_button)
        row.addWidget(self.ok_button)
        layout.addSpacing(s.xs)
        layout.addLayout(row)
        self.setFixedWidth(320 + 2 * self.SHADOW)
        self.hide()

    def show_hint(self, hint: Hint, target: QWidget) -> None:
        self.hint = hint
        self.title.setText(hint.title)
        self.text.setText(hint.text)
        self.setAccessibleName(f"Tip: {hint.title}. {hint.text}")
        self.adjustSize()
        anchor = target.mapTo(self._host, QPoint(0, target.height() + 2))
        x = max(0, min(anchor.x() - self.SHADOW, self._host.width() - self.width()))
        y = max(0, min(anchor.y() - self.SHADOW + 8, self._host.height() - self.height()))
        self.move(x, y)
        self.show()
        self.raise_()

    def _dismiss(self) -> None:
        if self.hint is not None:
            self.dismissed.emit(self.hint.id)
