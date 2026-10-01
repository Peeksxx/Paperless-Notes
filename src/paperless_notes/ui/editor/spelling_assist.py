"""Spelling in the editor. The highlighter underlines misspelled words. Right after a misspelled word is
typed (a space, punctuation or Enter follows it), its correction is drawn over it in faded text with a small
bar: Tab or Accept replaces the word, Esc or Keep leaves it and stops underlining it for this session, and
Add to dictionary keeps it for good. Right-clicking a misspelled word lists suggestions. The note changes
only when a correction is chosen, as one undo step."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QFontMetricsF, QKeyEvent, QPainter, QPen, QTextCursor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QMenu, QWidget

from paperless_notes.core.runtime import IOExecutor, Outcome
from paperless_notes.mdio.highlighter import SPELL_SKIP, MarkdownHighlighter
from paperless_notes.mdio.source import index_of, line_result, position_of
from paperless_notes.mdio.spelling import Speller, load_personal, save_personal
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.widgets import RowButton
from paperless_notes.ui.theme.tokens import Theme

logger = logging.getLogger(__name__)

WORD_CHARS = "'\N{RIGHT SINGLE QUOTATION MARK}"
GHOST_ALPHA = 0.5


class SpellService(QObject):
    """One word list and personal dictionary for every open note, loaded in the background."""

    changed = Signal()

    def __init__(self, dictionary_file: Path, enabled: bool = True, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.speller: Speller | None = None
        self.enabled = enabled
        self._file = dictionary_file

    @property
    def ready(self) -> bool:
        return self.enabled and self.speller is not None

    def start(self, executor: IOExecutor, loader: Callable[[], Speller] = Speller.bundled) -> None:
        path = self._file

        def job() -> Speller:
            speller = loader()
            speller.personal = load_personal(path)
            return speller

        executor.submit(job, self._loaded, kind="spelling")

    def _loaded(self, outcome: Outcome[Speller]) -> None:
        if outcome.error is not None or outcome.value is None:
            logger.warning("Spell checking is unavailable: %s", outcome.error)
            return
        self.speller = outcome.value
        self.changed.emit()

    def set_enabled(self, enabled: bool) -> None:
        if enabled != self.enabled:
            self.enabled = enabled
            self.changed.emit()

    def misspelled(self, text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
        speller = self.speller
        if not self.enabled or speller is None:
            return []
        return speller.misspelled(text, skip)

    def check(self, word: str) -> tuple[str | None, list[str]]:
        speller = self.speller
        return speller.check(word) if speller is not None else (None, [])

    def add(self, word: str) -> None:
        speller = self.speller
        if speller is None:
            return
        speller.add(word)
        try:
            save_personal(self._file, speller.personal)
        except OSError as exc:
            logger.warning("The personal dictionary could not be saved: %s", exc)
        self.changed.emit()

    def ignore(self, word: str) -> None:
        if self.speller is not None:
            self.speller.ignore(word)
            self.changed.emit()

    def attach(self, highlighter: MarkdownHighlighter) -> None:
        highlighter.set_spelling(self.misspelled)
        self.changed.connect(highlighter.spelling_changed)


@dataclass
class Pending:
    """A correction on offer: ``cursor`` spans the word and follows edits made elsewhere."""

    cursor: QTextCursor
    word: str
    fix: str
    caret: int


def is_separator(text: str) -> bool:
    """Whether typing ``text`` ends a word: a space, punctuation or a line break."""
    if text in ("\r", "\n"):
        return True
    return len(text) == 1 and text.isprintable() and not text.isalnum() and text not in WORD_CHARS


class SpellBar(QFrame):
    """Accept, Keep and Add to dictionary, under the word on offer."""

    def __init__(self, parent: QWidget, theme: Theme) -> None:
        super().__init__(parent)
        self.setObjectName("Hint")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 3, 4, 3)
        layout.setSpacing(2)
        self.accept_button = RowButton("Accept", "check", theme, "Tab")
        self.keep_button = RowButton("Keep", "close", theme, "Esc")
        self.add_button = RowButton("Add to dictionary", "plus", theme)
        self.accept_button.setToolTip("Use the correction (Tab)")
        self.keep_button.setToolTip("Keep your spelling and stop marking this word for now (Esc)")
        self.add_button.setToolTip("Add this word to your dictionary")
        for button in (self.accept_button, self.keep_button, self.add_button):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setFixedWidth(button.sizeHint().width())
            layout.addWidget(button)
        self.hide()

    def fit(self) -> None:
        """Size the buttons for their labels in the font the style sheet gives them."""
        for button in (self.accept_button, self.keep_button, self.add_button):
            button.ensurePolished()
            button.setFixedWidth(button.sizeHint().width())
        self.adjustSize()

    def apply_theme(self, theme: Theme) -> None:
        for button in (self.accept_button, self.keep_button, self.add_button):
            button.apply_theme(theme)


class SpellAssist(QObject):
    """Offers corrections in one editor view."""

    def __init__(self, editor: NoteEditor, service: SpellService) -> None:
        super().__init__(editor)
        self._editor = editor
        self._service = service
        theme = editor.theme
        self._theme = theme
        self.pending: Pending | None = None
        self.bar = SpellBar(editor.viewport(), theme)
        self.bar.accept_button.clicked.connect(self.accept)
        self.bar.keep_button.clicked.connect(self.keep)
        self.bar.add_button.clicked.connect(self.add_to_dictionary)
        editor.key_hooks.insert(0, self._on_key)
        editor.focus_out_hooks.append(self._on_focus_out)
        editor.paint_hooks.append(self._paint)
        editor.context_menu_hooks.append(self._context_menu)
        editor.cursorPositionChanged.connect(self._on_cursor)
        editor.theme_changed.connect(self._on_theme)

    def _on_theme(self) -> None:
        self._theme = self._editor.theme
        self.bar.apply_theme(self._theme)

    def _on_key(self, event: QKeyEvent) -> bool:
        if self.pending is not None:
            key = event.key()
            plain = not event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
            if key == Qt.Key.Key_Tab and plain:
                self.accept()
                return True
            if key == Qt.Key.Key_Escape:
                self.keep()
                return True
            if key in (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta):
                return False
            self.dismiss()
        text = event.text()
        control = event.modifiers() & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        if not control and is_separator(text):
            QTimer.singleShot(0, self, self.offer_at_caret)
        return False

    def offer_at_caret(self) -> None:
        """Offer a correction for the word just before the separator that was typed."""
        editor = self._editor
        if not self._service.ready or editor.isReadOnly():
            return
        caret = editor.textCursor()
        if caret.hasSelection():
            return
        block = caret.block()
        end = index_of(block, caret.position()) - 1
        if caret.positionInBlock() == 0 and block.previous().isValid():
            block = block.previous()
            end = len(block.text())
        text = block.text()
        start = end
        while start > 0 and (text[start - 1].isalpha() or text[start - 1] in WORD_CHARS):
            start -= 1
        while end > start and text[end - 1] in WORD_CHARS:
            end -= 1
        if end - start < 2:
            return
        result = line_result(block)
        skip = [(span.start, span.end) for span in result.spans if span.style & SPELL_SKIP]
        if (start, end) not in self._service.misspelled(text, skip):
            return
        word = text[start:end]
        fix, _suggestions = self._service.check(word)
        if fix is None or fix == word:
            return
        span = QTextCursor(editor.document())
        span.setPosition(position_of(block, start))
        span.setPosition(position_of(block, end), QTextCursor.MoveMode.KeepAnchor)
        self.pending = Pending(span, word, fix, caret.position())
        self._place_bar()
        editor.viewport().update()

    def _word_rect(self, pending: Pending) -> QRectF:
        cursor = pending.cursor
        block = cursor.document().findBlock(cursor.selectionStart())
        offset = cursor.selectionStart() - block.position()
        return self._editor.char_box(block, offset, cursor.selectionEnd() - cursor.selectionStart())

    def _font(self, pending: Pending) -> QFont:
        cursor = pending.cursor
        block = cursor.document().findBlock(cursor.selectionStart())
        return self._editor.font_at(block, cursor.selectionStart() - block.position())

    def _place_bar(self) -> None:
        pending = self.pending
        if pending is None:
            self.bar.hide()
            return
        rect = self._word_rect(pending)
        viewport = self._editor.viewport()
        self.bar.fit()
        x = min(max(4.0, rect.left() - 6), viewport.width() - self.bar.width() - 4)
        y = rect.bottom() + 6
        if y + self.bar.height() > viewport.height() - 4:
            y = rect.top() - self.bar.height() - 6
        self.bar.move(QPoint(round(x), round(y)))
        self.bar.show()
        self.bar.raise_()

    def _paint(self, painter: QPainter) -> None:
        pending = self.pending
        if pending is None or pending.cursor.selectedText() != pending.word:
            return
        p = self._theme.palette
        rect = self._word_rect(pending)
        font = self._font(pending)
        width = max(rect.width(), QFontMetricsF(font).horizontalAdvance(pending.fix))
        painter.save()
        painter.fillRect(QRectF(rect.left() - 1, rect.top(), width + 3, rect.height()), QColor(p.page))
        ghost = QColor(p.text)
        ghost.setAlphaF(GHOST_ALPHA)
        painter.setFont(font)
        painter.setPen(ghost)
        painter.drawText(
            QRectF(rect.left(), rect.top(), width + 2, rect.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            pending.fix,
        )
        line = QColor(p.accent)
        line.setAlphaF(0.6)
        pen = QPen(line, 1, Qt.PenStyle.DotLine)
        painter.setPen(pen)
        painter.drawLine(
            QPointF(rect.left(), rect.bottom() - 1), QPointF(rect.left() + width, rect.bottom() - 1)
        )
        painter.restore()

    def accept(self) -> None:
        pending = self.pending
        self.dismiss()
        if pending is not None:
            self._replace(pending.cursor, pending.word, pending.fix)

    def keep(self) -> None:
        pending = self.pending
        self.dismiss()
        if pending is not None:
            self._service.ignore(pending.word)

    def add_to_dictionary(self) -> None:
        pending = self.pending
        self.dismiss()
        if pending is not None:
            self._service.add(pending.word)

    def dismiss(self) -> None:
        if self.pending is not None:
            self.pending = None
            self.bar.hide()
            self._editor.viewport().update()

    def _on_cursor(self) -> None:
        pending = self.pending
        if pending is not None and self._editor.textCursor().position() != pending.caret:
            self.dismiss()

    def _on_focus_out(self) -> None:
        if not self.bar.underMouse():
            self.dismiss()

    def _replace(self, span: QTextCursor, word: str, fix: str) -> None:
        cursor = QTextCursor(span)
        if cursor.selectedText() != word or self._editor.isReadOnly():
            return
        cursor.beginEditBlock()
        cursor.insertText(fix)
        cursor.endEditBlock()

    def _context_menu(self, menu: QMenu, pos: QPoint) -> None:
        if not self._service.ready:
            return
        editor = self._editor
        at = editor.cursorForPosition(pos)
        block = at.block()
        column = index_of(block, at.position())
        text = block.text()
        result = line_result(block)
        skip = [(span.start, span.end) for span in result.spans if span.style & SPELL_SKIP]
        found = next(((a, b) for a, b in self._service.misspelled(text, skip) if a <= column <= b), None)
        if found is None:
            return
        start, end = found
        word = text[start:end]
        _fix, suggestions = self._service.check(word)
        span = QTextCursor(editor.document())
        span.setPosition(position_of(block, start))
        span.setPosition(position_of(block, end), QTextCursor.MoveMode.KeepAnchor)
        actions: list[QAction] = []
        for index, suggestion in enumerate(suggestions):
            action = QAction(suggestion, menu)
            if index == 0:
                bold = action.font()
                bold.setBold(True)
                action.setFont(bold)
            action.triggered.connect(partial(self._replace, QTextCursor(span), word, suggestion))
            actions.append(action)
        if not suggestions:
            empty = QAction("No suggestions", menu)
            empty.setEnabled(False)
            actions.append(empty)
        add = QAction("Add to dictionary", menu)
        add.triggered.connect(partial(self._service.add, word))
        actions.append(add)
        existing = menu.actions()
        if existing:
            menu.insertActions(existing[0], actions)
            menu.insertSeparator(existing[0])
        else:
            menu.addActions(actions)
