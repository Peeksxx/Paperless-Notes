"""Spelling in the editor. The highlighter underlines misspelled words. An obvious typo is corrected as soon
as the word ends (a space, punctuation or Enter), as its own undo step, and a small note under the word shows
the change: Esc puts the word back this time, Shift+Esc puts it back and adds it to the dictionary so it is
never corrected again. Ctrl+Z also puts it back. The note stays while you keep typing on that line and fades
after a few seconds.

Anything less certain stays underlined. Resting the pointer on an underlined word, or pressing Ctrl+Period
with the caret in it, opens its suggestions with Add to dictionary and Ignore; Up and Down move, Enter or Tab
applies and Esc closes. Right-clicking a misspelled word lists the same choices at the top of the menu. The
note changes only when a correction is made or chosen."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QEnterEvent,
    QFont,
    QFontMetrics,
    QKeyEvent,
    QPainter,
    QPaintEvent,
    QTextBlock,
    QTextCursor,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QVBoxLayout,
    QWidget,
)

from paperless_notes.core.runtime import IOExecutor, Outcome
from paperless_notes.mdio.highlighter import SPELL_SKIP, MarkdownHighlighter
from paperless_notes.mdio.source import index_of, line_result, position_of
from paperless_notes.mdio.spelling import Speller, load_personal, save_personal, starts_sentence
from paperless_notes.ui.editor.note_editor import NoteEditor
from paperless_notes.ui.shell.motion import Glow, faded, mix
from paperless_notes.ui.shell.popup import Popup
from paperless_notes.ui.shell.widgets import keycaps_width, meta_font, paint_keycaps
from paperless_notes.ui.theme.tokens import Theme

logger = logging.getLogger(__name__)

WORD_CHARS = "'\N{RIGHT SINGLE QUOTATION MARK}"
TRIGGERS = ' .,;:!?)]}"*~\N{RIGHT DOUBLE QUOTATION MARK}'
HOVER_MS = 450
LINGER_MS = 300
CHIP_MS = 5000
CHIP_GRACE_MS = 1500
MAX_SUGGESTIONS = 5
MODIFIER_KEYS = (Qt.Key.Key_Shift, Qt.Key.Key_Control, Qt.Key.Key_Alt, Qt.Key.Key_Meta)
ENTER_KEYS = (Qt.Key.Key_Return, Qt.Key.Key_Enter)


class SpellService(QObject):
    """One word list and personal dictionary for every open note, loaded in the background."""

    changed = Signal()

    def __init__(
        self,
        dictionary_file: Path,
        enabled: bool = True,
        parent: QObject | None = None,
        autocorrect: bool = True,
    ) -> None:
        super().__init__(parent)
        self.speller: Speller | None = None
        self.enabled = enabled
        self.autocorrect = autocorrect
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

    def set_autocorrect(self, autocorrect: bool) -> None:
        self.autocorrect = autocorrect

    def misspelled(self, text: str, skip: list[tuple[int, int]]) -> list[tuple[int, int]]:
        speller = self.speller
        if not self.enabled or speller is None:
            return []
        return speller.misspelled(text, skip)

    def check(self, word: str) -> tuple[str | None, list[str]]:
        speller = self.speller
        return speller.check(word, MAX_SUGGESTIONS) if speller is not None else (None, [])

    def autocorrection(self, word: str, sentence_start: bool) -> str | None:
        speller = self.speller
        if not self.ready or not self.autocorrect or speller is None:
            return None
        return speller.autocorrection(word, sentence_start)

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
class Typed:
    """The word before the caret when a key that ends words was pressed; ``start`` is at its first letter."""

    start: QTextCursor
    word: str
    revision: int
    sentence_start: bool


@dataclass
class Correction:
    """A word corrected as it was typed. ``end`` sits right after the fix and follows edits made elsewhere,
    so the fix (or, after Ctrl+Z, the original word) always ends there; ``caret`` is how far past it the caret
    was when the correction was made, and ``home`` sits there, on the line where typing goes on (the next one
    after Enter)."""

    end: QTextCursor
    word: str
    fix: str
    caret: int
    home: QTextCursor


@dataclass
class Target:
    """The underlined word the suggestions are for; ``span`` selects it."""

    span: QTextCursor
    word: str


class KeyHint(QAbstractButton):
    """Keycaps followed by what they do. Clicking does the same as pressing the keys."""

    def __init__(self, shortcut: str, text: str, theme: Theme) -> None:
        super().__init__()
        self._shortcut = shortcut
        self._theme = theme
        self.setText(text)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._glow = Glow(self, lambda: self._theme.ms(self._theme.motion.fast_ms))

    @property
    def keys(self) -> str:
        return self._shortcut

    def apply_theme(self, theme: Theme) -> None:
        self._theme = theme
        self.updateGeometry()
        self.update()

    def sizeHint(self) -> QSize:
        self.ensurePolished()
        keys = keycaps_width(self._shortcut, meta_font(self._theme))
        height = max(24, QFontMetrics(meta_font(self._theme)).height() + 10, self.fontMetrics().height() + 8)
        return QSize(keys + self.fontMetrics().horizontalAdvance(self.text()) + 20, height)

    def enterEvent(self, event: QEnterEvent) -> None:
        self._glow.set_on(True)
        super().enterEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        self._glow.set_on(False)
        super().leaveEvent(event)

    def paintEvent(self, _event: QPaintEvent) -> None:
        t = self._theme
        p = t.palette
        glow = self._glow.amount
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        fill = QColor(p.selected) if self.isDown() else faded(p.hover, glow)
        if fill.alpha() > 0:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(fill)
            painter.drawRoundedRect(QRectF(self.rect()), t.radius.sm, t.radius.sm)
        keys = paint_keycaps(painter, 6, self.height() / 2, self._shortcut, meta_font(t), p, align_left=True)
        painter.setFont(self.font())
        painter.setPen(mix(p.text_secondary, p.text, glow))
        painter.drawText(
            QRectF(keys + 12, 0, max(0, self.width() - keys - 12), self.height()),
            int(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft),
            self.text(),
        )


class CorrectionChip(Popup):
    """Under a word just corrected: what changed, and the keys that put the word back."""

    def __init__(self, host: QWidget, theme: Theme) -> None:
        super().__init__(host, theme)
        self.setAccessibleName("Spelling correction")
        s = theme.spacing
        layout = QHBoxLayout(self)
        layout.setContentsMargins(*self.inner_margins(s.md, s.xxs))
        layout.setSpacing(s.xs)
        self.label = QLabel("")
        self.label.setProperty("role", "secondary")
        self.undo_hint = KeyHint("Esc", "Undo", theme)
        self.add_hint = KeyHint("Shift+Esc", "Undo and add to dictionary", theme)
        layout.addWidget(self.label)
        layout.addSpacing(s.xs)
        layout.addWidget(self.undo_hint)
        layout.addWidget(self.add_hint)

    def apply_theme(self, theme: Theme) -> None:
        super().apply_theme(theme)
        self.undo_hint.apply_theme(theme)
        self.add_hint.apply_theme(theme)

    def show_change(self, word: str, fix: str) -> None:
        self.label.setText(f"{word} \N{RIGHTWARDS ARROW} {fix}")
        self.setAccessibleName(f"Corrected {word} to {fix}")
        self.undo_hint.setAccessibleName(f"Undo the correction of {word} (Esc)")
        self.undo_hint.setToolTip(f"Put back {word} this time")
        self.add_hint.setAccessibleName(f"Undo and add {word} to the dictionary (Shift+Esc)")
        self.add_hint.setToolTip(f"Put back {word} and keep it in your dictionary on this PC")


class SuggestionPopover(Popup):
    """Suggestions for one underlined word, then Add to dictionary and Ignore. The editor keeps the keyboard
    focus; ``SpellAssist`` moves through the rows and applies them."""

    chosen = Signal(str, str)

    ACTIONS = (
        ("Add to dictionary", "add", "Keep this spelling in your dictionary on this PC"),
        ("Ignore", "ignore", "Stop marking this word until Paperless Notes closes"),
    )

    def __init__(self, host: QWidget, theme: Theme) -> None:
        super().__init__(host, theme)
        self.setAccessibleName("Spelling suggestions")
        s = theme.spacing
        layout = QVBoxLayout(self)
        layout.setContentsMargins(*self.inner_margins(s.xs, s.xs))
        layout.setSpacing(s.xs)
        self.fixes = self._rows("Suggested spellings")
        self.rule = QFrame()
        self.rule.setObjectName("PopupRule")
        self.rule.setFixedHeight(1)
        self.options = self._rows("Spelling actions")
        for text, kind, tip in self.ACTIONS:
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, kind)
            item.setToolTip(tip)
            self.options.addItem(item)
        layout.addWidget(self.fixes)
        layout.addWidget(self.rule)
        layout.addWidget(self.options)

    def _rows(self, name: str) -> QListWidget:
        rows = QListWidget(self)
        rows.setProperty("role", "plain")
        rows.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        rows.setAccessibleName(name)
        rows.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rows.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rows.itemClicked.connect(self._clicked)
        return rows

    def set_suggestions(self, suggestions: list[str], editable: bool) -> None:
        self.fixes.clear()
        for index, suggestion in enumerate(suggestions):
            item = QListWidgetItem(suggestion)
            item.setData(Qt.ItemDataRole.UserRole, "fix")
            if index == 0:
                bold = QFont(self.fixes.font())
                bold.setBold(True)
                item.setFont(bold)
            if not editable:
                item.setFlags(Qt.ItemFlag.NoItemFlags)
            self.fixes.addItem(item)
        if not suggestions:
            empty = QListWidgetItem("No suggestions")
            empty.setFlags(Qt.ItemFlag.NoItemFlags)
            self.fixes.addItem(empty)
        self._fit()
        self.select(0)

    def labels(self) -> list[str]:
        return [item.text() for item in self._items(selectable=False)]

    def _items(self, selectable: bool = True) -> list[QListWidgetItem]:
        found: list[QListWidgetItem] = []
        for rows in (self.fixes, self.options):
            for i in range(rows.count()):
                item = rows.item(i)
                if not selectable or item.flags() & Qt.ItemFlag.ItemIsSelectable:
                    found.append(item)
        return found

    def current(self) -> QListWidgetItem | None:
        for rows in (self.fixes, self.options):
            item = rows.currentItem()
            if item is not None and item.isSelected():
                return item
        return None

    def select(self, index: int) -> None:
        items = self._items()
        if not items:
            return
        chosen = items[index % len(items)]
        for rows in (self.fixes, self.options):
            rows.clearSelection()
            if chosen.listWidget() is rows:
                rows.setCurrentItem(chosen)
            else:
                rows.setCurrentRow(-1)

    def step(self, delta: int) -> None:
        items = self._items()
        current = self.current()
        index = items.index(current) if current in items else (-1 if delta > 0 else 0)
        self.select(index + delta)

    def activate_current(self) -> None:
        item = self.current()
        if item is not None:
            self._clicked(item)

    def _clicked(self, item: QListWidgetItem) -> None:
        if item.flags() & Qt.ItemFlag.ItemIsSelectable:
            self.chosen.emit(str(item.data(Qt.ItemDataRole.UserRole)), item.text())

    def _fit(self) -> None:
        width = 0
        for rows in (self.fixes, self.options):
            rows.ensurePolished()
            width = max(width, rows.sizeHintForColumn(0) + 2 * rows.frameWidth() + 24)
        width = max(width, 180)
        for rows in (self.fixes, self.options):
            height = sum(rows.sizeHintForRow(i) for i in range(rows.count())) + 2 * rows.frameWidth()
            rows.setFixedSize(width, height)
        self.adjustSize()


class SpellAssist(QObject):
    """Corrects obvious typos and offers suggestions in one editor view."""

    def __init__(self, editor: NoteEditor, service: SpellService) -> None:
        super().__init__(editor)
        self._editor = editor
        self._service = service
        theme = editor.theme
        self.correction: Correction | None = None
        self.target: Target | None = None
        self._typed: Typed | None = None
        self._keyboard = False
        self._hovered: tuple[int, int, int] | None = None
        self._revision = editor.document().revision()
        self.chip = CorrectionChip(editor, theme)
        self.chip.undo_hint.clicked.connect(self.undo_correction)
        self.chip.add_hint.clicked.connect(self.undo_and_add)
        self.popover = SuggestionPopover(editor, theme)
        self.popover.chosen.connect(self._on_chosen)
        self._chip_timer = self._timer(CHIP_MS, self._chip_timeout)
        self._hover_timer = self._timer(HOVER_MS, self._open_hovered)
        self._linger_timer = self._timer(LINGER_MS, self._linger_check)
        self._linger_timer.setSingleShot(False)
        editor.viewport().setMouseTracking(True)
        editor.key_hooks.insert(0, self._on_key)
        editor.focus_out_hooks.append(self._on_focus_out)
        editor.context_menu_hooks.append(self._context_menu)
        editor.hover_hooks.append(self._on_hover)
        editor.cursorPositionChanged.connect(self._on_cursor)
        editor.textChanged.connect(self._on_text_changed)
        editor.theme_changed.connect(self._on_theme)
        editor.column_changed.connect(self._follow)
        editor.verticalScrollBar().valueChanged.connect(self._follow)

    def _timer(self, interval: int, slot: Callable[[], None]) -> QTimer:
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.setInterval(interval)
        timer.timeout.connect(slot)
        return timer

    def _on_theme(self) -> None:
        theme = self._editor.theme
        self.chip.apply_theme(theme)
        self.popover.apply_theme(theme)

    def _on_key(self, event: QKeyEvent) -> bool:
        key = event.key()
        modifiers = event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier
        plain = modifiers == Qt.KeyboardModifier.NoModifier
        if self.popover.is_open:
            if key == Qt.Key.Key_Escape:
                self.close_popover()
                return True
            if self._keyboard and key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and plain:
                self.popover.step(1 if key == Qt.Key.Key_Down else -1)
                return True
            if self._keyboard and (key in ENTER_KEYS or key == Qt.Key.Key_Tab) and plain:
                self.popover.activate_current()
                return True
            if key not in MODIFIER_KEYS:
                self.close_popover()
        if key == Qt.Key.Key_Period and modifiers == Qt.KeyboardModifier.ControlModifier:
            return self.suggest_at_caret()
        if key == Qt.Key.Key_Escape and self.chip.is_open and not self._other_popup_open():
            if modifiers == Qt.KeyboardModifier.ShiftModifier:
                self.undo_and_add()
                return True
            if plain:
                self.undo_correction()
                return True
        control = modifiers & (Qt.KeyboardModifier.ControlModifier | Qt.KeyboardModifier.AltModifier)
        if not control and (key in ENTER_KEYS or (len(event.text()) == 1 and event.text() in TRIGGERS)):
            self._note_typed()
        return False

    def _note_typed(self) -> None:
        """Remember the word just before the caret; it is corrected once the key has been applied."""
        editor = self._editor
        if not self._service.ready or not self._service.autocorrect or editor.isReadOnly():
            return
        caret = editor.textCursor()
        if caret.hasSelection():
            return
        block = caret.block()
        text = block.text()
        column = index_of(block, caret.position())
        if column < len(text) and (text[column].isalpha() or text[column] in WORD_CHARS):
            return
        start = column
        while start > 0 and (text[start - 1].isalpha() or text[start - 1] in WORD_CHARS):
            start -= 1
        end = column
        while end > start and text[end - 1] in WORD_CHARS:
            end -= 1
        if end - start < 3:
            return
        anchor = QTextCursor(editor.document())
        anchor.setPosition(position_of(block, start))
        self._typed = Typed(
            anchor, text[start:end], editor.document().revision(), starts_sentence(text[:start])
        )
        QTimer.singleShot(0, self, self._correct_typed)

    def _correct_typed(self) -> None:
        typed, self._typed = self._typed, None
        editor = self._editor
        if typed is None or editor.isReadOnly():
            return
        document = editor.document()
        if typed.start.document() is not document or document.revision() == typed.revision:
            return
        block = document.findBlock(typed.start.position())
        text = block.text()
        start = index_of(block, typed.start.position())
        end = start + len(typed.word)
        if text[start:end] != typed.word or (end < len(text) and text[end].isalpha()):
            return
        if (start, end) not in self._service.misspelled(text, self._skip(block)):
            return
        fix = self._service.autocorrection(typed.word, typed.sentence_start)
        if fix is None or fix == typed.word:
            return
        cursor = QTextCursor(document)
        cursor.setPosition(position_of(block, start))
        cursor.setPosition(position_of(block, end), QTextCursor.MoveMode.KeepAnchor)
        cursor.beginEditBlock()
        cursor.insertText(fix)
        cursor.endEditBlock()
        after = QTextCursor(document)
        after.setPosition(cursor.position())
        home = QTextCursor(document)
        home.setPosition(max(after.position(), editor.textCursor().position()))
        self.correction = Correction(after, typed.word, fix, home.position() - after.position(), home)
        self.chip.show_change(typed.word, fix)
        self._place_chip()
        self._chip_timer.start(CHIP_MS)

    def _corrected_text(self, correction: Correction) -> tuple[QTextBlock, int, str]:
        """The block holding the correction, the index where it ends, and the line's text."""
        end = correction.end.position()
        block = correction.end.document().findBlock(end)
        return block, index_of(block, end), block.text()

    def _place_chip(self) -> None:
        correction = self.correction
        if correction is None:
            return
        block, end, _text = self._corrected_text(correction)
        anchor = self._anchor(block, max(0, end - len(correction.fix)), end)
        if anchor is None:
            self.close_chip(at_once=True)
            return
        self.chip.present(anchor)

    def _box(self, block: QTextBlock, start: int, end: int) -> QRectF | None:
        """The word's box in viewport coordinates, or None when it is scrolled out of view."""
        editor = self._editor
        offset = position_of(block, start) - block.position()
        length = position_of(block, end) - position_of(block, start)
        box = editor.char_box(block, offset, max(1, length))
        if box.bottom() < 0 or box.top() > editor.viewport().height():
            return None
        return box

    def _anchor(self, block: QTextBlock, start: int, end: int) -> QRect | None:
        """The word's box in the editor's coordinates, where the popups live."""
        box = self._box(block, start, end)
        if box is None:
            return None
        editor = self._editor
        return QRect(editor.viewport().mapTo(editor, box.topLeft().toPoint()), box.size().toSize())

    def _other_popup_open(self) -> bool:
        """Esc goes first to a list open next to the text, such as the slash menu or the emoji list."""
        return any(
            isinstance(child, Popup) and child not in (self.chip, self.popover) and child.is_open
            for child in self._editor.children()
        )

    def undo_correction(self) -> None:
        """Put back the word as typed (Esc), as one undo step. The word is corrected again next time."""
        self._put_back(add=False)

    def undo_and_add(self) -> None:
        """Put back the word as typed (Shift+Esc), as one undo step, and add it to the dictionary."""
        self._put_back(add=True)

    def _put_back(self, add: bool) -> None:
        correction = self.correction
        self.close_chip(at_once=True)
        if correction is None or self._editor.isReadOnly():
            return
        block, end, text = self._corrected_text(correction)
        start = end - len(correction.fix)
        if start < 0 or text[start:end] != correction.fix:
            return
        cursor = QTextCursor(self._editor.document())
        cursor.setPosition(position_of(block, start))
        cursor.setPosition(position_of(block, end), QTextCursor.MoveMode.KeepAnchor)
        cursor.beginEditBlock()
        cursor.insertText(correction.word)
        cursor.endEditBlock()
        if add:
            self._service.add(correction.word)

    def close_chip(self, at_once: bool = False) -> None:
        self.correction = None
        self._chip_timer.stop()
        if at_once:
            self.chip.close_now()
        else:
            self.chip.dismiss()

    def _chip_timeout(self) -> None:
        if self.chip.underMouse():
            self._chip_timer.start(CHIP_GRACE_MS)
            return
        self.close_chip()

    def _check_correction(self) -> None:
        """Follow the corrected word through edits: Ctrl+Z restoring it puts the caret back where typing
        stopped, and any change to it closes the note."""
        correction = self.correction
        if correction is None:
            return
        _block, end, text = self._corrected_text(correction)
        if text[max(0, end - len(correction.fix)) : end] == correction.fix:
            self._place_chip()
            return
        self.close_chip()
        if text[max(0, end - len(correction.word)) : end] == correction.word:
            restored = correction.end.position()
            QTimer.singleShot(0, self, partial(self._caret_back, restored, restored + correction.caret))

    def _caret_back(self, restored: int, target: int) -> None:
        """Ctrl+Z leaves the caret at the end of the word it put back; return it to where typing stopped."""
        caret = self._editor.textCursor()
        if caret.hasSelection() or caret.position() != restored:
            return
        caret.setPosition(min(target, self._editor.document().characterCount() - 1))
        self._editor.setTextCursor(caret)

    def _on_text_changed(self) -> None:
        """Restyling also reports a change; only an edit moves the document's revision."""
        revision = self._editor.document().revision()
        if revision == self._revision:
            return
        self._revision = revision
        if self.popover.isVisible():
            self.close_popover()
        if self.correction is not None:
            self._check_correction()

    def _on_cursor(self) -> None:
        correction = self.correction
        if correction is not None:
            line = self._editor.textCursor().blockNumber()
            if line not in (correction.end.blockNumber(), correction.home.blockNumber()):
                self.close_chip()
        target = self.target
        if target is not None and self._keyboard:
            position = self._editor.textCursor().position()
            if not target.span.selectionStart() <= position <= target.span.selectionEnd():
                self.close_popover()

    def _follow(self, *_args: object) -> None:
        if self.correction is not None and self.chip.is_open:
            self._place_chip()
        if self.target is not None and self.popover.is_open:
            self._place_popover()

    def _on_focus_out(self) -> None:
        if not self.chip.underMouse():
            self.close_chip()
        if not self.popover.underMouse():
            self.close_popover()

    def _skip(self, block: QTextBlock) -> list[tuple[int, int]]:
        return [(span.start, span.end) for span in line_result(block).spans if span.style & SPELL_SKIP]

    def _misspelled_in(self, block: QTextBlock, column: int) -> tuple[int, int] | None:
        if not self._service.ready:
            return None
        found = self._service.misspelled(block.text(), self._skip(block))
        return next(((a, b) for a, b in found if a <= column <= b), None)

    def suggest_at_caret(self) -> bool:
        """Open the suggestions for the misspelled word at the caret (Ctrl+Period); False if there is none."""
        caret = self._editor.textCursor()
        block = caret.block()
        found = self._misspelled_in(block, index_of(block, caret.position()))
        if found is None:
            return False
        self._open_popover(block, *found, keyboard=True)
        return True

    def _word_at(self, pos: QPointF) -> tuple[QTextBlock, int, int] | None:
        """The misspelled word under the pointer, if the pointer is on its letters."""
        cursor = self._editor.cursorForPosition(pos.toPoint())
        block = cursor.block()
        found = self._misspelled_in(block, index_of(block, cursor.position()))
        if found is None:
            return None
        box = self._box(block, *found)
        if box is None or not box.adjusted(-1, -1, 1, 1).contains(pos):
            return None
        return block, found[0], found[1]

    @staticmethod
    def _key(found: tuple[QTextBlock, int, int] | None) -> tuple[int, int, int] | None:
        return (found[0].blockNumber(), found[1], found[2]) if found is not None else None

    def _on_hover(self, pos: QPointF) -> None:
        key = self._key(self._word_at(pos))
        if key is None:
            self._hover_timer.stop()
            self._hovered = None
        elif key != self._hovered and key != self._target_key():
            self._hovered = key
            self._hover_timer.start()

    def _target_span(self) -> tuple[QTextBlock, int, int] | None:
        target = self.target
        if target is None:
            return None
        block = target.span.document().findBlock(target.span.selectionStart())
        return (
            block,
            index_of(block, target.span.selectionStart()),
            index_of(block, target.span.selectionEnd()),
        )

    def _target_key(self) -> tuple[int, int, int] | None:
        return self._key(self._target_span()) if self.popover.is_open else None

    def _open_hovered(self) -> None:
        hovered = self._hovered
        if hovered is None or self._editor.textCursor().hasSelection():
            return
        number, start, end = hovered
        block = self._editor.document().findBlockByNumber(number)
        if block.isValid() and self._misspelled_in(block, start) == (start, end):
            self._open_popover(block, start, end, keyboard=False)

    def _open_popover(self, block: QTextBlock, start: int, end: int, keyboard: bool) -> None:
        word = block.text()[start:end]
        _fix, suggestions = self._service.check(word)
        span = QTextCursor(self._editor.document())
        span.setPosition(position_of(block, start))
        span.setPosition(position_of(block, end), QTextCursor.MoveMode.KeepAnchor)
        self.target = Target(span, word)
        self._keyboard = keyboard
        if keyboard:
            self._linger_timer.stop()
        else:
            self._linger_timer.start()
        self.popover.set_suggestions(suggestions, not self._editor.isReadOnly())
        self._place_popover()

    def _place_popover(self) -> None:
        span = self._target_span()
        if span is None:
            return
        anchor = self._anchor(*span)
        if anchor is None:
            self.close_popover()
            return
        self.popover.present(anchor)

    def close_popover(self) -> None:
        self.target = None
        self._keyboard = False
        self._linger_timer.stop()
        self.popover.dismiss()

    def _linger_check(self) -> None:
        """Suggestions opened by the pointer stay while it is on the word, on them or in the gap between,
        checked every ``LINGER_MS`` from where the pointer is (the text area reports no leaving to Python)."""
        span = self._target_span()
        if self._keyboard or not self.popover.is_open or span is None:
            self._linger_timer.stop()
            return
        editor = self._editor
        word = self._anchor(*span)
        panel = self.popover.panel_rect().translated(self.popover.pos())
        pointer = editor.mapFromGlobal(QCursor.pos())
        if word is None or not panel.united(word).contains(pointer):
            self.close_popover()

    def _on_chosen(self, kind: str, text: str) -> None:
        target = self.target
        self.close_popover()
        if target is None:
            return
        if kind == "fix":
            self._replace(target.span, target.word, text)
        elif kind == "add":
            self._service.add(target.word)
        elif kind == "ignore":
            self._service.ignore(target.word)

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
        found = self._misspelled_in(block, index_of(block, at.position()))
        if found is None:
            return
        start, end = found
        word = block.text()[start:end]
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
            action.setEnabled(not editor.isReadOnly())
            action.triggered.connect(partial(self._replace, QTextCursor(span), word, suggestion))
            actions.append(action)
        if not suggestions:
            empty = QAction("No suggestions", menu)
            empty.setEnabled(False)
            actions.append(empty)
        add = QAction("Add to dictionary", menu)
        add.triggered.connect(partial(self._service.add, word))
        actions.append(add)
        ignore = QAction("Ignore", menu)
        ignore.triggered.connect(partial(self._service.ignore, word))
        actions.append(ignore)
        existing = menu.actions()
        if existing:
            menu.insertActions(existing[0], actions)
            menu.insertSeparator(existing[0])
        else:
            menu.addActions(actions)
