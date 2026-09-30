"""The restrained status bar: words, line and column, zoom, encoding and line endings, compact sync state."""

from __future__ import annotations

from PySide6.QtCore import QPointF, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from paperless_notes.core import textformat
from paperless_notes.core.session import NoteSession
from paperless_notes.mdio.edits import count_tasks
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.widgets import paint_dot
from paperless_notes.ui.sync.page_sync import session_tone, short_state
from paperless_notes.ui.theme.tokens import Theme

WORD_COUNT_LIMIT = 1_000_000
_ENDINGS = {"\r\n": "CRLF", "\n": "LF", "\r": "CR"}


def format_label(fmt: textformat.TextFormat) -> str:
    encoding = "UTF-8 with BOM" if fmt.bom else "UTF-8"
    endings = "Mixed line endings" if fmt.mixed else _ENDINGS.get(fmt.newline, "LF")
    return f"{encoding}  {endings}"


def count_words(text: str) -> int | None:
    return None if len(text) > WORD_COUNT_LIMIT else len(text.split())


class StateDot(QWidget):
    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self._theme = theme
        self.tone = ""
        self.setFixedSize(18, 18)

    def set_tone(self, tone: str) -> None:
        self.tone = tone
        self.setVisible(bool(tone))
        self.update()

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        painter = QPainter(self)
        paint_dot(
            painter, QPointF(self.width() / 2 + 2, self.height() / 2), self.tone, self._theme.palette, 3.0
        )


class StatusBar(QWidget):
    zoom_reset_requested = Signal()

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self.setObjectName("StatusBar")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setFixedHeight(theme.metrics.status_bar)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(theme.spacing.sm, 0, theme.spacing.md, 0)
        layout.setSpacing(0)
        self.dot = StateDot(theme)
        self.dot.hide()
        layout.addWidget(self.dot)
        self.sync = QLabel("")
        self.sync.setAccessibleName("Save and sync state")
        self.words = QLabel("")
        self.words.setAccessibleName("Word count")
        self.tasks = QLabel("")
        self.tasks.setAccessibleName("Completed tasks")
        self.tasks.setToolTip("Completed task boxes in this note")
        self.position = QLabel("")
        self.position.setAccessibleName("Line and column")
        self.format = QLabel("")
        self.format.setAccessibleName("Encoding and line endings")
        self.format.setToolTip("Encoding and line endings are kept exactly as the file has them")
        self.zoom = QToolButton()
        self.zoom.setAccessibleName("Zoom")
        self.zoom.setToolTip("Zoom of the note text. Click to reset to 100% (Ctrl+0)")
        self.zoom.setProperty("kind", "labelled")
        self.zoom.clicked.connect(self.zoom_reset_requested.emit)
        layout.addWidget(self.sync)
        layout.addStretch(1)
        for widget in (self.tasks, self.words, self.position, self.format):
            layout.addWidget(widget)
        layout.addWidget(self.zoom)
        self._editor: NoteEditor | None = None
        self._session: NoteSession | None = None
        self._has_tasks = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(300)
        self._timer.timeout.connect(self._count)
        self.show_note(None, None)

    def _fit(self) -> None:
        """When the bar is narrow, the encoding and then the word count step aside so nothing is clipped."""
        if self._editor is None:
            return
        optional = (self.format, self.words)
        for widget in optional:
            widget.setVisible(True)
        fixed = [self.dot, self.sync, self.position, self.zoom, *((self.tasks,) if self._has_tasks else ())]
        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else None
        spare = self.width() - (margins.left() + margins.right() if margins is not None else 0)
        needed = sum(w.sizeHint().width() for w in (*fixed, *optional))
        for widget in optional:
            if needed <= spare:
                break
            widget.setVisible(False)
            needed -= widget.sizeHint().width()

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._fit()

    def show_note(self, editor: NoteEditor | None, session: NoteSession | None) -> None:
        if self._editor is not None:
            self._editor.cursorPositionChanged.disconnect(self._update_position)
            self._editor.textChanged.disconnect(self._timer.start)
            self._editor.zoom_changed.disconnect(self._update_zoom)
        self._editor = editor
        self._session = session
        has_note = editor is not None and session is not None
        for widget in (self.words, self.position, self.format, self.zoom):
            widget.setVisible(has_note)
        if editor is None or session is None:
            self.sync.setText("")
            self.dot.set_tone("")
            self.tasks.hide()
            return
        editor.cursorPositionChanged.connect(self._update_position)
        editor.textChanged.connect(self._timer.start)
        editor.zoom_changed.connect(self._update_zoom)
        self._update_position()
        self._update_zoom(editor.zoom)
        self._count()
        self.refresh_session()

    def refresh_session(self) -> None:
        session = self._session
        if session is None:
            return
        self.sync.setText(short_state(session))
        self.dot.set_tone(session_tone(session) if self.sync.text() else "")
        self.format.setText(format_label(session.text_format))
        self._fit()

    def apply_theme(self, theme: Theme) -> None:
        self.dot.apply_theme(theme)

    def _update_position(self) -> None:
        if self._editor is None:
            return
        cursor = self._editor.textCursor()
        self.position.setText(f"Ln {cursor.blockNumber() + 1}, Col {cursor.positionInBlock() + 1}")

    def _update_zoom(self, percent: int) -> None:
        self.zoom.setText(f"{percent}%")

    def _count(self) -> None:
        if self._editor is None:
            return
        count = count_words(self._editor.toPlainText())
        self.words.setText("Large note" if count is None else f"{count} word{'s' if count != 1 else ''}")
        tasks = count_tasks(self._editor.document())
        self._has_tasks = tasks is not None and tasks[1] > 0
        if tasks is not None and tasks[1] > 0:
            self.tasks.setText(f"{tasks[0]} of {tasks[1]} task{'s' if tasks[1] != 1 else ''}")
        self.tasks.setVisible(self._has_tasks)
        self._fit()
