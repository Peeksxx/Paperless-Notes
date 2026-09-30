"""The editor model: the note's source text in one shared ``QTextDocument``.

The document holds the only live copy of the text. Views (tabs, split panes) attach to the same
document, so they share undo history and the session. External reloads replace only the changed
middle of the text and clear the undo stack, so Ctrl+Z can never silently revert another PC's edit.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QTextCharFormat, QTextCursor, QTextDocument, QTextFormat

from paperless_notes.core.editor import ChangeListener, common_affixes, text_sha
from paperless_notes.mdio.document import SafeTextDocument

BLOCK_SEPARATOR = "\N{PARAGRAPH SEPARATOR}"
LARGE_DOCUMENT_UNITS = 1_000_000
LARGE_DOCUMENT_UNDO_STEPS = 200
LINE_ID_PROPERTY = QTextFormat.Property.UserProperty.value + 1


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


class TextDocumentAdapter(QObject):
    read_only_changed = Signal(bool)

    def __init__(self, document: QTextDocument | None = None) -> None:
        super().__init__()
        self.document = document if document is not None else SafeTextDocument()
        self._listener: ChangeListener | None = None
        self._programmatic = 0
        self._read_only = False
        self._base_sha = text_sha("")
        self._base_units = 0
        self._modified_rev = -1
        self._modified = False
        self._endings: dict[int, str] | None = None
        self.document.contentsChange.connect(self._on_contents_change)

    @contextmanager
    def _program(self) -> Iterator[None]:
        self._programmatic += 1
        try:
            yield
        finally:
            self._programmatic -= 1

    def _set_base(self, text: str) -> None:
        self._base_sha = text_sha(text)
        self._base_units = utf16_len(text)
        self._modified_rev = -1

    def load(self, text: str) -> None:
        self._endings = None
        with self._program():
            self.document.setPlainText(text)
        self.document.clearUndoRedoStacks()
        self.document.setModified(False)
        self._set_base(text)

    def text(self) -> str:
        return self.document.toRawText().replace(BLOCK_SEPARATOR, "\n")

    def apply_external(self, text: str, keep_cursor: bool = True) -> None:
        old = self.text()
        if old != text:
            prefix, suffix = common_affixes(old, text)
            with self._program():
                cursor = QTextCursor(self.document)
                cursor.beginEditBlock()
                cursor.setPosition(utf16_len(old[:prefix]))
                cursor.setPosition(utf16_len(old[: len(old) - suffix]), QTextCursor.MoveMode.KeepAnchor)
                cursor.insertText(text[prefix : len(text) - suffix])
                cursor.endEditBlock()
        self._endings = None
        self.document.clearUndoRedoStacks()
        self.document.setModified(False)
        self._set_base(text)

    def track_line_endings(self, endings: tuple[str, ...] | None) -> None:
        """Remember each line break's original characters (mixed-ending files only).

        The block character format belongs to the break character that starts each block, so the tag
        survives edits around it, is restored by undo and is absent on breaks the user creates.
        """
        if not endings or len(set(endings)) < 2:
            self._endings = None
            return
        with self._program():
            block = self.document.firstBlock().next()
            cursor = QTextCursor(self.document)
            index = 0
            while block.isValid() and index < len(endings):
                fmt = QTextCharFormat(block.charFormat())
                fmt.setProperty(LINE_ID_PROPERTY, index)
                cursor.setPosition(block.position())
                cursor.setBlockCharFormat(fmt)
                block = block.next()
                index += 1
        self.document.clearUndoRedoStacks()
        self._endings = dict(enumerate(endings))
        self._modified_rev = -1

    def line_endings(self) -> list[str | None] | None:
        if self._endings is None:
            return None
        result: list[str | None] = []
        seen: set[int] = set()
        block = self.document.firstBlock().next()
        while block.isValid():
            value = block.charFormat().property(LINE_ID_PROPERTY)
            if isinstance(value, int) and value not in seen:
                seen.add(value)
                result.append(self._endings.get(value))
            else:
                result.append(None)
            block = block.next()
        return result

    def set_base(self, text: str) -> None:
        self._set_base(text)

    def is_user_modified(self) -> bool:
        revision = self.document.revision()
        if revision == self._modified_rev:
            return self._modified
        if self.document.characterCount() - 1 != self._base_units:
            modified = True
        else:
            modified = text_sha(self.text()) != self._base_sha
        self._modified_rev = revision
        self._modified = modified
        return modified

    @property
    def read_only(self) -> bool:
        return self._read_only

    def set_read_only(self, read_only: bool) -> None:
        if read_only != self._read_only:
            self._read_only = read_only
            self.read_only_changed.emit(read_only)

    def set_change_listener(self, listener: ChangeListener | None) -> None:
        self._listener = listener

    def drop_undo_history(self) -> None:
        """Free undo memory, used for clean notes that have not been touched for a long time."""
        self.document.clearUndoRedoStacks()

    def _on_contents_change(self, position: int, removed: int, added: int) -> None:
        if self._programmatic or (removed == 0 and added == 0):
            return
        if (
            self.document.characterCount() > LARGE_DOCUMENT_UNITS
            and self.document.availableUndoSteps() > LARGE_DOCUMENT_UNDO_STEPS
        ):
            self.document.clearUndoRedoStacks(QTextDocument.Stacks.UndoStack)
        if self._listener is not None:
            self._listener()
