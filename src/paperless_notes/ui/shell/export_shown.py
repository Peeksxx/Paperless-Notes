"""Export as shown: the note drawn the way its editor shows it, in the current theme, note font, zoom and
column width, under its title, as PDF pages on US Letter or as one long PNG image.

A copy of the note is laid out in an editor that is never shown on screen, at the same text width as the
editor it comes from, and painted with the editor's own drawing (bullets, task boxes, quote bars, code
panels, rules), so the look is not imitated but reused. No line shows its Markdown marks, as if the caret
were elsewhere, and spelling marks are left out. Pages break between lines, never through one. The note
itself is not touched; this runs on the UI thread because it paints widgets."""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, replace

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMarginsF, QPoint, QRect, QRectF, Qt
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetricsF,
    QImage,
    QImageWriter,
    QPageLayout,
    QPageSize,
    QPainter,
    QPdfWriter,
    QRegion,
    QTextDocument,
)
from PySide6.QtWidgets import QPlainTextDocumentLayout, QWidget

from paperless_notes.branding import PRODUCT_NAME
from paperless_notes.mdio.highlighter import MarkdownHighlighter
from paperless_notes.ui.editor.note_editor import NoteEditor, highlight_theme
from paperless_notes.ui.shell.widgets import display_font
from paperless_notes.ui.theme.tokens import Theme

MARGIN = 48
TITLE_GAP = 20
PNG_SCALE = 2
MAX_PNG_HEIGHT = 16_000
PDF_DPI = 96
STATIC_FACES = {
    "Segoe UI Variable": "Segoe UI",
    "Segoe UI Variable Display": "Segoe UI",
    "Cascadia Mono": "Consolas",
}
_HEADING = re.compile(r"^ {0,3}#{1,6}(?:\s|$)")
_HEADING_TEXT = re.compile(r"^ {0,3}#{1,6}\s+(.*?)(?:\s+#+)?\s*$")


def repeats_title(text: str, title: str) -> bool:
    """Whether the note's first line is a heading that already says `title`, so it need not be drawn twice."""
    first = next((line for line in text.splitlines() if line.strip()), "")
    found = _HEADING_TEXT.match(first)
    return found is not None and found.group(1).strip().casefold() == title.strip().casefold()


class TooLongForImageError(ValueError):
    """The note would make an image taller than image viewers handle well."""


@dataclass(frozen=True, slots=True)
class ShownView:
    """How a note looks in its editor: theme, note font, zoom, the width its text wraps at, and its title."""

    theme: Theme
    note_font: str
    zoom: int
    text_width: int
    title: str

    @classmethod
    def of(cls, editor: NoteEditor, title: str) -> ShownView:
        return cls(editor.theme, editor.note_font, editor.zoom, editor.viewport().width(), title)


def static_faces(families: tuple[str, ...]) -> tuple[str, ...]:
    """The same faces without variable fonts, which the PDF writer embeds at their heaviest default."""
    return tuple(dict.fromkeys(STATIC_FACES.get(f, f) for f in families))


def static_theme(current: Theme) -> Theme:
    t = current.typography
    return replace(
        current,
        typography=replace(
            t,
            ui_families=static_faces(t.ui_families),
            display_families=static_faces(t.display_families),
            mono_families=static_faces(t.mono_families),
        ),
    )


class ShownNote:
    """The note laid out at its editor's text width, tall enough to hold all of it, ready to paint in
    pieces. Call ``close`` when done."""

    def __init__(self, text: str, view: ShownView, static: bool = False) -> None:
        self.view = view
        self.title = "" if repeats_title(text, view.title) else view.title
        self.theme = static_theme(view.theme) if static else view.theme
        self.document = QTextDocument()
        self.document.setDocumentLayout(QPlainTextDocumentLayout(self.document))
        self.document.setPlainText(text)
        editor = NoteEditor(self.document, self.theme, None, None, view.note_font, 0)
        editor.drives_reveal = False
        editor.setAttribute(Qt.WidgetAttribute.WA_DontShowOnScreen, True)
        editor.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        editor.set_zoom(view.zoom)
        if static:
            editor.set_font_families(static_faces(editor.font_families))
        self.highlighter = MarkdownHighlighter(
            self.document,
            highlight_theme(self.theme, view.note_font, editor.point_size, editor.font_families),
            conceal=True,
        )
        editor.set_highlighter(self.highlighter)
        editor.set_width_cap(view.text_width)
        editor.resize(view.text_width + 4 * self.theme.metrics.gutter, 600)
        editor.show()
        self.highlighter.rehighlight()
        self.editor = editor
        self.lines = list(self._line_spans())
        self.height = self.lines[-1][1] + round(self.document.documentMargin()) if self.lines else 0
        editor.resize(editor.width(), max(1, self.height) + 1)

    @property
    def width(self) -> int:
        return self.editor.viewport().width()

    def _line_spans(self) -> Iterator[tuple[int, int, bool]]:
        """Top and bottom of every drawn line, in the editor's text area, top to bottom, and whether it is
        the last line of a heading."""
        layout = self.document.documentLayout()
        block = self.document.firstBlock()
        top = self.editor.blockBoundingGeometry(block).top() if block.isValid() else 0.0
        while block.isValid():
            height = layout.blockBoundingRect(block).height()
            lines = block.layout()
            heading = bool(_HEADING.match(block.text()))
            if lines is not None:
                origin = top + lines.position().y()
                count = lines.lineCount()
                for index in range(count):
                    line = lines.lineAt(index).rect()
                    yield (
                        round(origin + line.top()),
                        round(origin + line.bottom()),
                        heading and index == count - 1,
                    )
            top += height
            block = block.next()

    def page_breaks(self, page_height: int, first_page_height: int) -> list[tuple[int, int]]:
        """(top, bottom) of the text on each page. Every page ends after a whole line, and a heading moves to
        the next page with the text under it rather than ending a page on its own."""
        pages: list[tuple[int, int]] = []
        start = 0
        room = first_page_height
        end = start
        last_top = start
        last_heading = False
        for top, bottom, heading in self.lines:
            if bottom - start > room and end > start:
                cut = last_top if last_heading and last_top > start else end
                pages.append((start, cut))
                start, room = cut, page_height
            while bottom - start > room:
                pages.append((start, start + room))
                start, room = start + room, page_height
            end = bottom
            last_top, last_heading = top, heading
        pages.append((start, max(end, self.height)))
        return pages

    def paint_text(self, painter: QPainter, at: QPoint, top: int, bottom: int) -> None:
        """Paint the text between ``top`` and ``bottom`` of the text area with its top left at ``at``."""
        source = QRegion(QRect(0, top, self.width, max(1, bottom - top)))
        self.editor.viewport().render(painter, at, source, QWidget.RenderFlag.DrawWindowBackground)

    def title_height(self) -> int:
        if not self.title:
            return 0
        rect = QFontMetricsF(self._title_font()).boundingRect(
            QRectF(0, 0, self.width, 10_000), int(Qt.TextFlag.TextWordWrap), self.title
        )
        return round(rect.height()) + TITLE_GAP

    def paint_title(self, painter: QPainter, at: QPoint) -> None:
        if not self.title:
            return
        painter.save()
        painter.setFont(self._title_font())
        painter.setPen(QColor(self.theme.palette.text))
        painter.drawText(
            QRectF(at.x(), at.y(), self.width, self.title_height()),
            int(Qt.TextFlag.TextWordWrap) | int(Qt.AlignmentFlag.AlignLeft) | int(Qt.AlignmentFlag.AlignTop),
            self.title,
        )
        painter.restore()

    def _title_font(self) -> QFont:
        return display_font(self.theme, self.theme.typography.title_pt)

    def close(self) -> None:
        self.highlighter.setDocument(None)
        self.editor.release_document()
        self.editor.close()
        self.editor.deleteLater()


def shown_png(text: str, view: ShownView) -> bytes:
    """One long PNG at twice the size on screen, the page colour all round."""
    note = ShownNote(text, view)
    try:
        head = note.title_height()
        width = note.width + 2 * MARGIN
        height = MARGIN + head + note.height + MARGIN
        if height > MAX_PNG_HEIGHT:
            raise TooLongForImageError(
                "This note is too long for one image. Export it as a PDF as shown instead. "
                "Nothing was written."
            )
        image = QImage(width * PNG_SCALE, height * PNG_SCALE, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(PNG_SCALE)
        image.fill(QColor(note.theme.palette.page))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
        note.paint_title(painter, QPoint(MARGIN, MARGIN))
        note.paint_text(painter, QPoint(MARGIN, MARGIN + head), 0, note.height)
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        QImageWriter(buffer, QByteArray(b"png")).write(image)
        return bytes(buffer.data().data())
    finally:
        note.close()


def shown_pdf(text: str, view: ShownView) -> bytes:
    """US Letter pages filled with the page colour, the text column scaled down only if it is wider than
    the page, and the title on the first page."""
    note = ShownNote(text, view, static=True)
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    writer = QPdfWriter(buffer)
    try:
        writer.setTitle(view.title or PRODUCT_NAME)
        writer.setCreator(PRODUCT_NAME)
        writer.setResolution(PDF_DPI)
        writer.setPageLayout(
            QPageLayout(
                QPageSize(QPageSize.PageSizeId.Letter),
                QPageLayout.Orientation.Portrait,
                QMarginsF(0, 0, 0, 0),
            )
        )
        page = writer.pageLayout().fullRectPixels(PDF_DPI)
        scale = min(1.0, page.width() / (note.width + 2 * MARGIN))
        room = int(page.height() / scale) - 2 * MARGIN
        head = note.title_height()
        pages = note.page_breaks(room, max(1, room - head))
        painter = QPainter(writer)
        for index, (top, bottom) in enumerate(pages):
            if index:
                writer.newPage()
            painter.fillRect(
                QRect(0, 0, page.width() + 1, page.height() + 1), QColor(note.theme.palette.page)
            )
            painter.save()
            painter.scale(scale, scale)
            y = MARGIN
            if index == 0:
                note.paint_title(painter, QPoint(MARGIN, MARGIN))
                y += head
            note.paint_text(painter, QPoint(MARGIN, y), top, bottom)
            painter.restore()
        painter.end()
    finally:
        del writer
        note.close()
    return bytes(buffer.data().data())
