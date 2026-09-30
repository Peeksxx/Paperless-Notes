"""The note editor: the source text in a centered reading column with live-preview decorations.

Everything painted here (bullets, task boxes, quote bars, rules, code panels, table lines, change marks) is
drawn over the document; nothing is inserted into it. Clicking a task box edits exactly one character
through a normal text cursor, which is one undo step and goes through the session's usual save path.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

from PySide6.QtCore import QMimeData, QPointF, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor,
    QFocusEvent,
    QFont,
    QInputMethodEvent,
    QKeyEvent,
    QMouseEvent,
    QPainter,
    QPainterPath,
    QPaintEvent,
    QPalette,
    QPen,
    QResizeEvent,
    QTextBlock,
    QTextCursor,
    QTextDocument,
    QWheelEvent,
)
from PySide6.QtWidgets import QPlainTextEdit, QToolTip, QWidget

from paperless_notes.core.security.links import open_link
from paperless_notes.mdio.highlighter import BlockDecor, HighlightTheme, MarkdownHighlighter, lexer_state
from paperless_notes.mdio.links import link_at
from paperless_notes.ui.theme.tokens import NOTE_FONTS, Theme

ZOOM_STEPS = (70, 80, 90, 100, 110, 125, 150, 175, 200)


@dataclass(frozen=True, slots=True)
class ChangeMark:
    """A range of lines (0-based, end exclusive) that changed elsewhere, with a short before/after preview."""

    first: int
    last: int
    before: str
    after: str


class _Gutter(QWidget):
    """Left margin that shows 'changed elsewhere' marks. It never takes keyboard focus."""

    def __init__(self, editor: NoteEditor) -> None:
        super().__init__(editor)
        self._editor = editor
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        self._editor.paint_gutter(self, event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        mark = self._editor.mark_at(event.position().y())
        if mark is None:
            QToolTip.hideText()
            return
        QToolTip.showText(event.globalPosition().toPoint(), self._editor.mark_tooltip(mark), self)


def highlight_theme(theme: Theme, note_font: str, point_size: float) -> HighlightTheme:
    p = theme.palette
    return HighlightTheme(
        text=p.text,
        marker=p.marker,
        muted=p.text_muted,
        accent=p.accent,
        link=p.link,
        code_background=p.code_background,
        quote=p.quote,
        html=p.text_muted,
        base_point_size=point_size,
        body_families=NOTE_FONTS.get(note_font, NOTE_FONTS["sans"]),
        mono_family=theme.typography.mono_families[0],
    )


class NoteEditor(QPlainTextEdit):
    zoom_changed = Signal(int)
    marks_changed = Signal(int)
    column_changed = Signal(int, int)
    focused = Signal()

    def __init__(
        self,
        document: QTextDocument,
        theme: Theme,
        highlighter: MarkdownHighlighter | None = None,
        opener: Callable[[str], bool] | None = None,
        note_font: str = "sans",
        readable_width: int | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setDocument(document)
        self.setFrameShape(QPlainTextEdit.Shape.NoFrame)
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)
        self.setAccessibleName("Note text")
        self._highlighter = highlighter
        self._opener = opener
        self._theme = theme
        self._note_font = note_font
        self._readable = readable_width if readable_width is not None else theme.metrics.readable_width
        self._zoom = 100
        self._marks: list[ChangeMark] = []
        self.drives_reveal = True
        self._paste_handler: Callable[[QMimeData, bool], bool] | None = None
        self.key_hooks: list[Callable[[QKeyEvent], bool]] = []
        self.focus_out_hooks: list[Callable[[], None]] = []
        self.input_method_hooks: list[Callable[[QInputMethodEvent], None]] = []
        self._gutter = _Gutter(self)
        self.cursorPositionChanged.connect(self._on_cursor_moved)
        self.apply_theme(theme)
        self._on_cursor_moved()

    @property
    def highlighter(self) -> MarkdownHighlighter | None:
        return self._highlighter

    def set_paste_handler(self, handler: Callable[[QMimeData, bool], bool] | None) -> None:
        """Paste and drop go through ``handler`` first (smart paste, images); False falls back to Qt."""
        self._paste_handler = handler

    def canInsertFromMimeData(self, source: QMimeData) -> bool:  # noqa: N802 - Qt override
        if self._paste_handler is not None and (source.hasUrls() or source.hasImage()):
            return True
        return super().canInsertFromMimeData(source)

    def insertFromMimeData(self, source: QMimeData) -> None:  # noqa: N802 - Qt override
        if self._paste_handler is not None and self._paste_handler(source, False):
            return
        super().insertFromMimeData(source)

    def set_highlighter(self, highlighter: MarkdownHighlighter | None) -> None:
        """Attach live preview once the note is loaded and small enough; styling only, never the text."""
        self._highlighter = highlighter
        self._apply_font()
        self._on_cursor_moved()

    def release_document(self, detach_highlighter: bool = True) -> None:
        """Detach from the note's document before this view is destroyed.

        PySide hands a document to C++ ownership when a view adopts it, yet Qt only deletes documents a
        view created itself, so an adopted document would never be freed. Detaching the highlighter and
        taking ownership back lets the document go with its adapter (measured by tools/ui_memcheck.py).
        When another view still shows the document (split view), ``detach_highlighter`` is False: the
        shared highlighter stays with the document and only this view lets go of it.
        """
        document = self.document()
        if self._highlighter is not None and detach_highlighter:
            self._highlighter.setDocument(None)
        self._highlighter = None
        self.setDocument(QTextDocument(self))
        document.setParent(None)

    @property
    def zoom(self) -> int:
        return self._zoom

    @property
    def point_size(self) -> float:
        return self._theme.typography.note_pt * self._zoom / 100

    def apply_theme(self, theme: Theme, note_font: str | None = None) -> None:
        """Colors and fonts only: no undo step, no text change."""
        self._theme = theme
        if note_font is not None:
            self._note_font = note_font
        p = theme.palette
        palette = self.palette()
        palette.setColor(QPalette.ColorRole.Base, QColor(p.page))
        palette.setColor(QPalette.ColorRole.Window, QColor(p.page))
        palette.setColor(QPalette.ColorRole.Text, QColor(p.text))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(p.selection))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor(p.selection_text))
        palette.setColor(QPalette.ColorRole.PlaceholderText, QColor(p.text_muted))
        self.setAutoFillBackground(True)
        self.setPalette(palette)
        self.viewport().setPalette(palette)
        self._apply_font()

    def set_note_font(self, note_font: str) -> None:
        self._note_font = note_font
        self._apply_font()

    def set_readable_width(self, width: int) -> None:
        self._readable = width
        self._update_margins()

    def _apply_font(self) -> None:
        font = QFont()
        font.setFamilies(list(NOTE_FONTS.get(self._note_font, NOTE_FONTS["sans"])))
        font.setPointSizeF(self.point_size)
        self.document().setDefaultFont(font)
        self.setFont(font)
        if self._highlighter is not None:
            self._highlighter.set_theme(highlight_theme(self._theme, self._note_font, self.point_size))
        self._update_margins()

    def set_zoom(self, percent: int) -> None:
        percent = max(ZOOM_STEPS[0], min(ZOOM_STEPS[-1], percent))
        if percent != self._zoom:
            self._zoom = percent
            self._apply_font()
            self.zoom_changed.emit(percent)

    def zoom_in(self) -> None:
        self.set_zoom(next((z for z in ZOOM_STEPS if z > self._zoom), ZOOM_STEPS[-1]))

    def zoom_out(self) -> None:
        self.set_zoom(next((z for z in reversed(ZOOM_STEPS) if z < self._zoom), ZOOM_STEPS[0]))

    def _update_margins(self) -> None:
        gutter = self._theme.metrics.gutter
        available = self.width() - self.verticalScrollBar().sizeHint().width()
        column = int(self._readable * self._zoom / 100) if self._readable > 0 else available
        side = max(gutter, (available - column) // 2)
        right = max(0, side - gutter // 2)
        self.setViewportMargins(side, 0, right, 0)
        cr = self.contentsRect()
        self._gutter.setGeometry(QRect(cr.left() + side - gutter, cr.top(), gutter, cr.height()))
        self.column_changed.emit(side, max(0, self.width() - side - right))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 - Qt override
        super().resizeEvent(event)
        self._update_margins()

    def _on_cursor_moved(self) -> None:
        if self._highlighter is not None and self.drives_reveal:
            self._highlighter.set_active_block(self.textCursor().blockNumber())
        self.viewport().update()

    def focusInEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - Qt override
        super().focusInEvent(event)
        self.focused.emit()

    def decor(self, block: QTextBlock) -> BlockDecor | None:
        return self._highlighter.decor_for(block) if self._highlighter is not None else None

    def visible_blocks(self) -> Iterator[tuple[QTextBlock, QRectF]]:
        block = self.firstVisibleBlock()
        offset = self.contentOffset()
        height = self.viewport().height()
        while block.isValid():
            geometry = self.blockBoundingGeometry(block).translated(offset)
            if geometry.top() > height:
                break
            if block.isVisible():
                yield block, geometry
            block = block.next()

    def char_box(self, block: QTextBlock, offset: int, length: int) -> QRectF:
        cursor = QTextCursor(block)
        cursor.setPosition(block.position() + offset)
        start = self.cursorRect(cursor)
        cursor.setPosition(block.position() + offset + length)
        end = self.cursorRect(cursor)
        right = end.left() if end.top() == start.top() else start.left() + start.height()
        return QRectF(start.left(), start.top(), max(1.0, right - start.left()), start.height())

    def task_boxes(self) -> list[tuple[QRectF, QTextBlock, int, bool]]:
        found: list[tuple[QRectF, QTextBlock, int, bool]] = []
        for block, _ in self.visible_blocks():
            decor = self.decor(block)
            if decor is not None:
                for offset, checked in decor.tasks:
                    found.append((self.char_box(block, offset, 3), block, offset, checked))
        return found

    def paintEvent(self, event: QPaintEvent) -> None:  # noqa: N802 - Qt override
        p = self._theme.palette
        painter = QPainter(self.viewport())
        width = self.viewport().width()
        for block, geometry in self.visible_blocks():
            decor = self.decor(block)
            if decor is not None and decor.code:
                painter.fillRect(
                    QRectF(0, geometry.top(), width, geometry.height()), QColor(p.code_background)
                )
        painter.end()
        super().paintEvent(event)
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        for block, geometry in self.visible_blocks():
            decor = self.decor(block)
            if decor is not None:
                self._paint_decor(painter, block, geometry, decor, width)
        painter.end()

    def _paint_decor(
        self, painter: QPainter, block: QTextBlock, geometry: QRectF, decor: BlockDecor, width: float
    ) -> None:
        p = self._theme.palette
        for offset in decor.bullets:
            box = self.char_box(block, offset, 1)
            radius = max(2.0, box.height() * 0.11)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.text_secondary))
            painter.drawEllipse(QPointF(box.center().x(), box.center().y()), radius, radius)
        for offset, checked in decor.tasks:
            self._paint_task(painter, self.char_box(block, offset, 3), checked)
        for offset in decor.quotes:
            box = self.char_box(block, offset, 1)
            painter.fillRect(
                QRectF(box.center().x() - 1.5, geometry.top(), 3, geometry.height()), QColor(p.border_strong)
            )
        if decor.rule:
            painter.setPen(QPen(QColor(p.border_strong), 1))
            y = geometry.center().y()
            painter.drawLine(QPointF(4, y), QPointF(width - 4, y))
        if decor.table:
            painter.setPen(QPen(QColor(p.border), 1))
            painter.drawLine(QPointF(4, geometry.bottom() - 0.5), QPointF(width - 4, geometry.bottom() - 0.5))

    def _paint_task(self, painter: QPainter, box: QRectF, checked: bool) -> None:
        p = self._theme.palette
        side = min(box.height() * 0.62, box.width())
        square = QRectF(box.center().x() - side / 2, box.center().y() - side / 2, side, side)
        radius = self._theme.radius.sm * side / 16
        if checked:
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(p.accent))
            painter.drawRoundedRect(square, radius, radius)
            tick = QPainterPath()
            tick.moveTo(square.left() + side * 0.25, square.top() + side * 0.52)
            tick.lineTo(square.left() + side * 0.43, square.top() + side * 0.70)
            tick.lineTo(square.left() + side * 0.76, square.top() + side * 0.32)
            painter.setPen(QPen(QColor(p.on_accent), max(1.5, side / 9)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(tick)
        else:
            painter.setPen(QPen(QColor(p.border_strong), max(1.0, side / 14)))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawRoundedRect(square, radius, radius)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt override
        if event.button() == Qt.MouseButton.LeftButton:
            modifiers = event.modifiers()
            if modifiers == Qt.KeyboardModifier.NoModifier and self._toggle_task_at(event.position()):
                event.accept()
                return
            if modifiers & Qt.KeyboardModifier.ControlModifier and self._open_link_at(event.position()):
                event.accept()
                return
        super().mousePressEvent(event)

    def _toggle_task_at(self, pos: QPointF) -> bool:
        if self.isReadOnly():
            return False
        for box, block, offset, checked in self.task_boxes():
            if box.contains(pos):
                self.toggle_task(block, offset, checked)
                return True
        return False

    def toggle_task(self, block: QTextBlock, offset: int, checked: bool) -> None:
        """Replace only the character inside ``[ ]`` or ``[x]``: one undo step, caret untouched."""
        position = block.position() + offset + 1
        cursor = QTextCursor(self.document())
        cursor.setPosition(position)
        cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
        cursor.beginEditBlock()
        cursor.insertText(" " if checked else "x")
        cursor.endEditBlock()

    def link_at_point(self, pos: QPointF) -> str | None:
        cursor = self.cursorForPosition(pos.toPoint())
        block = cursor.block()
        previous = block.previous()
        state = lexer_state(previous.userState()) if previous.isValid() else -1
        return link_at(block.text(), cursor.positionInBlock(), max(state, 0))

    def _open_link_at(self, pos: QPointF) -> bool:
        url = self.link_at_point(pos)
        if url is None or self._opener is None:
            return False
        return open_link(url, self._opener)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 - Qt override
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.angleDelta().y() > 0:
                self.zoom_in()
            elif event.angleDelta().y() < 0:
                self.zoom_out()
            event.accept()
            return
        super().wheelEvent(event)

    def keyPressEvent(self, event: QKeyEvent) -> None:  # noqa: N802 - Qt override
        for hook in list(self.key_hooks):
            if hook(event):
                event.accept()
                return
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            if event.key() in (Qt.Key.Key_Plus, Qt.Key.Key_Equal):
                self.zoom_in()
                return
            if event.key() == Qt.Key.Key_Minus:
                self.zoom_out()
                return
            if event.key() == Qt.Key.Key_0:
                self.set_zoom(100)
                return
        super().keyPressEvent(event)

    def focusOutEvent(self, event: QFocusEvent) -> None:  # noqa: N802 - Qt override
        super().focusOutEvent(event)
        for hook in list(self.focus_out_hooks):
            hook()

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:  # noqa: N802 - Qt override
        for hook in list(self.input_method_hooks):
            hook(event)
        super().inputMethodEvent(event)

    def set_change_marks(self, marks: list[ChangeMark]) -> None:
        self._marks = list(marks)
        self._gutter.update()
        self.marks_changed.emit(len(self._marks))

    def clear_change_marks(self) -> None:
        self.set_change_marks([])

    @property
    def change_marks(self) -> list[ChangeMark]:
        return list(self._marks)

    def next_mark(self, step: int = 1) -> ChangeMark | None:
        """Move the caret to the next (or previous) mark and show its before and after text."""
        if not self._marks:
            return None
        line = self.textCursor().blockNumber()
        ordered = sorted(self._marks, key=lambda m: m.first)
        if step > 0:
            mark = next((m for m in ordered if m.first > line), ordered[0])
        else:
            mark = next((m for m in reversed(ordered) if m.first < line), ordered[-1])
        block = self.document().findBlockByNumber(min(mark.first, self.document().blockCount() - 1))
        self.setTextCursor(QTextCursor(block))
        self.ensureCursorVisible()
        rect = self.cursorRect()
        QToolTip.showText(self.viewport().mapToGlobal(rect.bottomLeft()), self.mark_tooltip(mark), self)
        return mark

    def mark_at(self, y: float) -> ChangeMark | None:
        for block, geometry in self.visible_blocks():
            if geometry.top() <= y <= geometry.bottom():
                number = block.blockNumber()
                return next((m for m in self._marks if m.first <= number < max(m.last, m.first + 1)), None)
        return None

    def mark_tooltip(self, mark: ChangeMark) -> str:
        before = mark.before or "(nothing)"
        after = mark.after or "(removed)"
        return f"Changed elsewhere\nBefore:\n{before}\nNow:\n{after}"

    def paint_gutter(self, gutter: QWidget, event: QPaintEvent) -> None:
        if not self._marks:
            return
        painter = QPainter(gutter)
        color = QColor(self._theme.palette.changed_mark)
        x = gutter.width() - 8
        for block, geometry in self.visible_blocks():
            number = block.blockNumber()
            if any(m.first <= number < max(m.last, m.first + 1) for m in self._marks):
                painter.fillRect(QRectF(x, geometry.top() + 2, 3, max(2.0, geometry.height() - 4)), color)
        painter.end()

    def sizeHint(self) -> QSize:  # noqa: N802 - Qt override
        return QSize(self._readable + 2 * self._theme.metrics.gutter, 400)
