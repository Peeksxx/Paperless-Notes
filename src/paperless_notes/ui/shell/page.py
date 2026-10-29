"""One open note: the editable title, the sync line under it, banners, 'changed elsewhere' controls and the
editor. Header and banners share the editor's reading column so the page reads as one document."""

from __future__ import annotations

import ntpath
from collections.abc import Callable

from PySide6.QtCore import QMimeData, QObject, Qt, QTimer, Signal
from PySide6.QtGui import QFocusEvent, QKeyEvent, QResizeEvent, QTextCursor, QTextOption
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core.local_state import NoteColors
from paperless_notes.core.security.filenames import NameCheck
from paperless_notes.core.session import NoteSession
from paperless_notes.ui.editor.authoring import Authoring, AuthoringHost
from paperless_notes.ui.editor.authoring_ui import AuthoringBar, FormatBubble, SlashMenu, menu_button
from paperless_notes.ui.editor.find_bar import FindBar
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.flow import FlowLayout
from paperless_notes.ui.shell.motion import SlideOut
from paperless_notes.ui.sync.page_sync import SyncHooks, SyncPresenter
from paperless_notes.ui.theme.qss import page_stylesheet
from paperless_notes.ui.theme.tokens import Theme, note_theme

EMPTY_NOTE_HINT = "Use [/] for quick slash commands!"


def title_of(path: str) -> str:
    return ntpath.splitext(ntpath.basename(path))[0]


class TitleField(QPlainTextEdit):
    """The file name without its extension, wrapped rather than clipped. Enter renames, Escape restores,
    and a name problem shows before anything changes on disk."""

    rename_requested = Signal(str)

    def __init__(self, validate: Callable[[str], NameCheck] | None = None) -> None:
        super().__init__()
        self.setObjectName("TitleField")
        self.setAccessibleName("Note title, also the file name")
        self.setToolTip("Note title. Edit and press Enter to rename the file")
        self.setTabChangesFocus(True)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._validate = validate
        self._current = ""
        self._setting = False
        self.problem = QLabel("")
        self.problem.setWordWrap(True)
        self.problem.setProperty("role", "secondary")
        self.problem.hide()
        self.textChanged.connect(self._on_changed)

    def text(self) -> str:
        return self.toPlainText()

    def set_title(self, text: str) -> None:
        self._current = text
        self._setting = True
        self.setPlainText(text)
        self._setting = False
        self.moveCursor(QTextCursor.MoveOperation.Start)
        self.problem.hide()
        self._fit()

    def _on_changed(self) -> None:
        self._fit()
        if not self._setting:
            self._check(self.text())

    def _fit(self) -> None:
        lines = max(1.0, self.document().size().height())
        margins = self.contentsMargins()
        height = lines * self.fontMetrics().lineSpacing() + 2 * self.document().documentMargin()
        self.setFixedHeight(int(height) + margins.top() + margins.bottom() + 2)

    def _check(self, text: str) -> NameCheck | None:
        if self._validate is None or text.strip() == self._current:
            self.problem.hide()
            return None
        check = self._validate(text)
        self.problem.setText("" if check.ok else check.problem or "")
        self.problem.setVisible(not check.ok)
        return check

    def commit(self) -> None:
        text = " ".join(self.text().split())
        if not text or text == self._current:
            self.set_title(self._current)
            return
        check = self._check(text)
        if check is not None and not check.ok:
            return
        self.rename_requested.emit(text)

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.commit()
            return
        if event.key() == Qt.Key.Key_Escape:
            self.set_title(self._current)
            return
        super().keyPressEvent(event)

    def insertFromMimeData(self, source: QMimeData) -> None:
        self.insertPlainText(" ".join(source.text().split()))

    def focusOutEvent(self, event: QFocusEvent) -> None:
        super().focusOutEvent(event)
        if self.text().strip() != self._current:
            self.commit()
        self.moveCursor(QTextCursor.MoveOperation.Start)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()


class MarksBar(QFrame):
    def __init__(self, editor: NoteEditor) -> None:
        super().__init__()
        self._editor = editor
        layout = FlowLayout(self)
        self.label = QLabel("")
        self.label.setProperty("role", "secondary")
        self.next_button = QPushButton("Show next change")
        self.next_button.setProperty("kind", "quiet")
        self.next_button.setToolTip("Go to the next line changed elsewhere and show what it was (Alt+F5)")
        self.next_button.clicked.connect(lambda: editor.next_mark(1))
        self.clear_button = QPushButton("Clear marks")
        self.clear_button.setProperty("kind", "quiet")
        self.clear_button.setToolTip("Hide the 'changed elsewhere' marks; the text is not changed")
        self.clear_button.clicked.connect(editor.clear_change_marks)
        layout.addWidget(self.label)
        layout.addWidget(self.next_button)
        layout.addWidget(self.clear_button)
        editor.marks_changed.connect(self._update)
        self._update(0)

    def _update(self, count: int) -> None:
        self.label.setText(f"{count} place{'s' if count != 1 else ''} changed elsewhere")
        self.setVisible(count > 0)


class NotePage(QWidget):
    """One open note. ``colors`` is the note's own (background, text) pair, drawn over the default theme."""

    theme_requested = Signal()

    def __init__(
        self,
        session: NoteSession,
        editor: NoteEditor,
        theme: Theme,
        hooks: SyncHooks,
        validate: Callable[[str], NameCheck] | None = None,
        authoring_host: AuthoringHost | None = None,
        release: Callable[[NoteEditor], None] | None = None,
        menu: Callable[[NotePage], QMenu] | None = None,
        export_menu: Callable[[NotePage], QMenu] | None = None,
        colors: NoteColors | None = None,
    ) -> None:
        super().__init__()
        self.setObjectName("PageArea")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.session = session
        self.editor = editor
        self.colors = colors
        self._base = theme
        self._sheet = ""
        editor.setPlaceholderText(EMPTY_NOTE_HINT)
        self._release = release
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.header = QWidget()
        self.header.setObjectName("PageHeader")
        self.header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        head = QVBoxLayout(self.header)
        self._head = head
        s = theme.spacing
        head.setSpacing(s.xs)
        self.title = TitleField(validate)
        self.title.set_title(title_of(session.path))
        self.sync_line = QLabel("")
        self.sync_line.setObjectName("SyncLine")
        self.sync_line.setWordWrap(True)
        self.notice = QLabel("")
        self.notice.setObjectName("SyncLine")
        self.notice.setProperty("state", "needs_review")
        self.notice.setWordWrap(True)
        self.banner_host = QVBoxLayout()
        self.banner_host.setSpacing(s.sm)
        self.marks_bar = MarksBar(editor)
        head.addWidget(self.title)
        head.addWidget(self.title.problem)
        head.addWidget(self.sync_line)
        head.addWidget(self.notice)
        head.addLayout(self.banner_host)
        head.addWidget(self.marks_bar)
        self.header_scroll = QScrollArea()
        self.header_scroll.setObjectName("PageHeaderScroll")
        self.header_scroll.setWidget(self.header)
        self.header_scroll.setWidgetResizable(True)
        self.header_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.header_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.header_scroll.viewport().setAutoFillBackground(False)
        layout.addWidget(self.header_scroll)
        self.authoring = Authoring(editor, authoring_host)
        self.authoring_bar = AuthoringBar(self.authoring, theme)
        self._bar_row = QHBoxLayout()
        self._bar_row.setSpacing(0)
        self._bar_row.addWidget(self.authoring_bar)
        format_menu = self.authoring_bar.format_menu
        format_menu.addSeparator()
        self.theme_action = format_menu.addAction("Theme...")
        self.theme_action.setToolTip("Choose this note's background and text colours")
        self.theme_action.triggered.connect(lambda _checked=False: self.theme_requested.emit())
        self.note_button: QToolButton | None = None
        self.export_button: QToolButton | None = None
        if menu is not None:
            self.note_button = menu_button(
                "Note", "Find and replace, and the outline, for this note", menu(self)
            )
            self.authoring_bar.add_trailing(self.note_button)
        if export_menu is not None:
            self.export_button = menu_button(
                "Export", "Save this note as a web page, PDF, text or image, or copy it", export_menu(self)
            )
            self.authoring_bar.add_trailing(self.export_button)
        layout.addLayout(self._bar_row)
        self.find_bar = FindBar(editor, theme)
        self._find_row = QHBoxLayout()
        self.find_box = SlideOut(
            self.find_bar, Qt.Edge.TopEdge, lambda: self._theme.ms(self._theme.motion.normal_ms)
        )
        self._find_row.addWidget(self.find_box)
        layout.addLayout(self._find_row)
        layout.addWidget(editor, 1)
        self.slash_menu = SlashMenu(editor, self.authoring)
        self.bubble = FormatBubble(editor, self.authoring)
        self._fit_timer = QTimer(self)
        self._fit_timer.setSingleShot(True)
        self._fit_timer.timeout.connect(self._fit_header)
        self._theme = theme
        editor.column_changed.connect(self._align)
        self._path_connection = session.path_changed.connect(self._on_path_changed)
        self.presenter = SyncPresenter(
            session, editor, self.sync_line, self.banner_host, theme, hooks, self.notice
        )
        self.presenter.changed.connect(self._fit_timer.start)
        self.title.textChanged.connect(self._fit_timer.start)
        editor.marks_changed.connect(self._fit_timer.start)
        self._align(theme.metrics.gutter, editor.width())
        if colors is not None:
            self.apply_theme(theme)

    def _align(self, left: int, width: int) -> None:
        s = self._theme.spacing
        right = max(0, self.editor.width() - left - width)
        self._head.setContentsMargins(max(0, left - 1), s.xxl, right, s.md)
        self._bar_row.setContentsMargins(max(0, left - 1), 0, right, s.xs)
        self._find_row.setContentsMargins(max(0, left - 1), 0, right, 0)
        self._fit_timer.start()

    def _fit_header(self) -> None:
        """The header takes what it needs, but never more than half the page; beyond that it scrolls."""
        wanted = self._head.totalHeightForWidth(max(1, self.header_scroll.viewport().width()))
        if wanted < 0:
            wanted = self.header.sizeHint().height()
        cap = max(self._theme.metrics.control * 3, self.height() // 2)
        self.header_scroll.setFixedHeight(min(wanted, cap))

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit_header()

    def _on_path_changed(self, path: str) -> None:
        self.title.set_title(title_of(path))

    @property
    def theme(self) -> Theme:
        """The theme the page is drawn in: the default theme, or the note's own colours over it."""
        return self._theme

    def apply_theme(self, theme: Theme) -> None:
        """Show ``theme``, or the note's own colours drawn over it when the note has them."""
        self._base = theme
        shown = theme if self.colors is None else note_theme(theme, *self.colors)
        self._theme = shown
        self.editor.apply_theme(shown)
        self.presenter.set_theme(shown)
        self.find_bar.apply_theme(shown)
        sheet = "" if self.colors is None else page_stylesheet(shown)
        if sheet != self._sheet:
            self._sheet = sheet
            self.setStyleSheet(sheet)

    def set_colors(self, colors: NoteColors | None) -> None:
        """Change the note's own colours, or go back to the default theme with None."""
        self.colors = colors
        self.apply_theme(self._base)

    def dispose(self) -> None:
        """Disconnect from the session and give the note's document back before this page goes away."""
        self.presenter.dispose()
        QObject.disconnect(self._path_connection)
        self.find_bar.dispose()
        self.bubble.dispose()
        self.slash_menu.dispose()
        self.authoring_bar.dispose()
        self.authoring.dispose()
        if self._release is not None:
            self._release(self.editor)
        else:
            self.editor.release_document()
