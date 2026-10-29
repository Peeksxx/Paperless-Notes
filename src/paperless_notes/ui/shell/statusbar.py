"""The restrained status bar: words, line and column, zoom, encoding and line endings, compact sync state."""

from __future__ import annotations

import re
from collections.abc import Callable

from PySide6.QtCore import QAbstractAnimation, QPointF, QRectF, Qt, QTimer, QVariantAnimation, Signal
from PySide6.QtGui import QColor, QPainter, QPaintEvent, QPen, QResizeEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QWidget

from paperless_notes.core import textformat
from paperless_notes.core.session import NoteSession, SessionState
from paperless_notes.mdio.edits import count_tasks
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.motion import Tween
from paperless_notes.ui.shell.widgets import ToneLabel
from paperless_notes.ui.sync.page_sync import session_tone, short_state
from paperless_notes.ui.theme.tokens import Theme

WORD_COUNT_LIMIT = 1_000_000
_ENDINGS = {"\r\n": "CRLF", "\n": "LF", "\r": "CR"}
_WORD = re.compile(r"\S*[^\W_]\S*")
_LINE_START = r"^[ \t]*(?:>[ \t]*)*"
_MARKS = (
    re.compile(_LINE_START + r"\d{1,9}[.)](?=\s)", re.MULTILINE),
    re.compile(_LINE_START + r"(?:[-*+]|\d{1,9}[.)])[ \t]+\[[xX]\](?=\s|$)", re.MULTILINE),
    re.compile(r"^[ \t]*(?:`{3,}|~{3,})[ \t]*[^\s`~]*[^\W_]", re.MULTILINE),
)
SPIN_DELAY_MS = 150
SPIN_MS = 900
IDLE, SAVING, SAVED = "", "saving", "saved"


class SaveMark(QWidget):
    """Before the save state: a small arc that turns while a save takes longer than a moment, and a check
    that stays for as long as the note is saved; after a save it draws itself. With Reduce motion the arc
    stands still and the check appears at once."""

    def __init__(self, theme: Theme) -> None:
        super().__init__()
        self._theme = theme
        self.state = IDLE
        self.setFixedSize(16, 16)
        self.setAccessibleName("Saving")
        self._angle = 0.0
        self._spin = QVariantAnimation(self)
        self._spin.setStartValue(0.0)
        self._spin.setEndValue(360.0)
        self._spin.setDuration(SPIN_MS)
        self._spin.setLoopCount(-1)
        self._spin.valueChanged.connect(self._turn)
        self._shown = Tween(self, 0.0, lambda _value: self.update())
        self._drawn = Tween(self, 0.0, lambda _value: self.update())
        self._delay = self._timer(SPIN_DELAY_MS, self._begin_spinning)

    def _timer(self, interval: int, slot: Callable[[], None]) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval)
        timer.timeout.connect(slot)
        return timer

    @property
    def spinning(self) -> bool:
        return self._spin.state() == QAbstractAnimation.State.Running

    @property
    def opacity(self) -> float:
        return float(self._shown.value)

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.update()

    def _ms(self, value: int) -> int:
        return self._theme.ms(value)

    def _turn(self, value: object) -> None:
        self._angle = float(value) if isinstance(value, float | int) else 0.0
        self.update()

    def saving(self) -> None:
        """A save started; the arc shows only if it is still running after a moment."""
        if self.state == SAVING:
            return
        self.state = SAVING
        self._drawn.jump(0.0)
        self._delay.start()

    def _begin_spinning(self) -> None:
        if self.state != SAVING:
            return
        self._shown.jump(1.0)
        if self._ms(SPIN_MS) > 0:
            self._spin.start()
        self.update()

    def saved(self, drawn: bool = True) -> None:
        """The note is saved: the check stays. Right after a save it draws itself (`drawn`); a note that was
        already saved shows it at once."""
        was_saved = self.state == SAVED
        self.state = SAVED
        self._delay.stop()
        self._spin.stop()
        self._shown.jump(1.0)
        if was_saved:
            return
        if drawn:
            self._drawn.jump(0.0)
            self._drawn.to(1.0, self._ms(self._theme.motion.normal_ms))
        else:
            self._drawn.jump(1.0)

    def clear(self) -> None:
        self.state = IDLE
        self._delay.stop()
        self._spin.stop()
        self._shown.jump(0.0)
        self.update()

    def paintEvent(self, _event: QPaintEvent) -> None:
        if self.opacity <= 0.0 or self.state == IDLE:
            return
        p = self._theme.palette
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setOpacity(self.opacity)
        box = QRectF(self.rect()).adjusted(3, 3, -3, -3)
        if self.state == SAVING:
            pen = QPen(QColor(p.state_busy), 1.6)
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            painter.setPen(pen)
            painter.drawArc(box, round(-self._angle * 16), 270 * 16)
            return
        pen = QPen(QColor(p.state_ok), 1.8)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter.setPen(pen)
        start = QPointF(box.left() + box.width() * 0.1, box.top() + box.height() * 0.55)
        corner = QPointF(box.left() + box.width() * 0.4, box.top() + box.height() * 0.85)
        end = QPointF(box.right(), box.top() + box.height() * 0.15)
        first = (corner - start).manhattanLength()
        second = (end - corner).manhattanLength()
        drawn = float(self._drawn.value) * (first + second)
        painter.drawLine(start, start + (corner - start) * min(1.0, drawn / first))
        if drawn > first:
            painter.drawLine(corner, corner + (end - corner) * min(1.0, (drawn - first) / second))


def format_label(fmt: textformat.TextFormat) -> str:
    encoding = "UTF-8 with BOM" if fmt.bom else "UTF-8"
    endings = "Mixed line endings" if fmt.mixed else _ENDINGS.get(fmt.newline, "LF")
    return f"{encoding}  {endings}"


def count_words(text: str) -> int | None:
    """Words as a reader counts them: a word has a letter or a digit, so Markdown marks on their own (list
    dashes, heading hashes, quote marks, table pipes, rules, fences, empty task boxes) do not count, and
    neither do list numbers, ticked task boxes and the language name after a code fence. None for very large
    notes."""
    if len(text) > WORD_COUNT_LIMIT:
        return None
    return len(_WORD.findall(text)) - sum(len(mark.findall(text)) for mark in _MARKS)


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
        self.mark = SaveMark(theme)
        self.sync = ToneLabel(theme)
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
        layout.addWidget(self.mark)
        layout.addWidget(self.sync)
        layout.addStretch(1)
        for widget in (self.tasks, self.words, self.position, self.format):
            layout.addWidget(widget)
        layout.addWidget(self.zoom)
        self._editor: NoteEditor | None = None
        self._session: NoteSession | None = None
        self._last_state: SessionState | None = None
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
        fixed = [self.mark, self.sync, self.position, self.zoom, *((self.tasks,) if self._has_tasks else ())]
        layout = self.layout()
        margins = layout.contentsMargins() if layout is not None else None
        spare = self.width() - (margins.left() + margins.right() if margins is not None else 0)
        needed = sum(w.sizeHint().width() for w in (*fixed, *optional))
        for widget in optional:
            if needed <= spare:
                break
            widget.setVisible(False)
            needed -= widget.sizeHint().width()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._fit()

    def show_note(self, editor: NoteEditor | None, session: NoteSession | None) -> None:
        if self._editor is not None:
            self._editor.cursorPositionChanged.disconnect(self._update_position)
            self._editor.textChanged.disconnect(self._timer.start)
            self._editor.zoom_changed.disconnect(self._update_zoom)
        self._editor = editor
        self._session = session
        self.mark.clear()
        self._last_state = session.state if session is not None else None
        has_note = editor is not None and session is not None
        for widget in (self.words, self.position, self.format, self.zoom):
            widget.setVisible(has_note)
        if editor is None or session is None:
            self.sync.setText("")
            self.sync.set_tone("")
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
        self._follow_saving(session)
        self.sync.setText(short_state(session))
        self.sync.set_tone(session_tone(session) if self.sync.text() else "")
        self.format.setText(format_label(session.text_format))
        self._fit()

    def _follow_saving(self, session: NoteSession) -> None:
        state = session.state
        if state is SessionState.SAVING:
            self.mark.saving()
        elif state is SessionState.READY and session_tone(session) == "ok":
            self.mark.saved(drawn=self._last_state is SessionState.SAVING)
        else:
            self.mark.clear()
        self._last_state = state

    def apply_theme(self, theme: Theme) -> None:
        self.sync.apply_theme(theme)
        self.mark.apply_theme(theme)

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
