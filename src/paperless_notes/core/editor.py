"""The contract between a note session and whatever holds the editable text."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Protocol

type ChangeListener = Callable[[], None]


def text_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "surrogatepass")).hexdigest()


class EditorAdapter(Protocol):
    """Editable text for one note. "Modified" means the text differs from the base; programmatic
    loads never count as user edits, and undoing back to the base is not modified."""

    def load(self, text: str) -> None:
        """Replace everything, reset undo history and set the base."""
        ...

    def text(self) -> str: ...

    def apply_external(self, text: str, keep_cursor: bool = True) -> None:
        """Replace the text programmatically (reload or merge) and set the base."""
        ...

    def set_base(self, text: str) -> None: ...

    def is_user_modified(self) -> bool: ...

    def set_read_only(self, read_only: bool) -> None: ...

    def set_change_listener(self, listener: ChangeListener | None) -> None: ...

    def track_line_endings(self, endings: tuple[str, ...] | None) -> None:
        """Follow each original line break through edits (only needed for mixed-ending files)."""
        ...

    def line_endings(self) -> list[str | None] | None:
        """Original break per line break of :meth:`text`, None where the break is new."""
        ...


def common_affixes(old: str, new: str) -> tuple[int, int]:
    """Length of the common prefix and of the common suffix that does not overlap it."""
    limit = min(len(old), len(new))
    prefix = 0
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    return prefix, suffix


def map_offset(offset: int, old: str, new: str) -> int:
    """Where a cursor at ``offset`` in ``old`` should land in ``new``."""
    prefix, suffix = common_affixes(old, new)
    if offset <= prefix:
        return offset
    if offset >= len(old) - suffix:
        return offset + len(new) - len(old)
    return min(prefix + (offset - prefix), len(new) - suffix)


class PlainTextBuffer:
    """Headless :class:`EditorAdapter` used by tests, the simulation and tools."""

    def __init__(self, text: str = "") -> None:
        self._text = text
        self._base_sha = text_sha(text)
        self._listener: ChangeListener | None = None
        self.cursor = 0
        self.read_only = False

    def load(self, text: str) -> None:
        self._text = text
        self._base_sha = text_sha(text)
        self.cursor = min(self.cursor, len(text))

    def text(self) -> str:
        return self._text

    def apply_external(self, text: str, keep_cursor: bool = True) -> None:
        self.cursor = map_offset(self.cursor, self._text, text) if keep_cursor else 0
        self._text = text
        self._base_sha = text_sha(text)

    def set_base(self, text: str) -> None:
        self._base_sha = text_sha(text)

    def is_user_modified(self) -> bool:
        return text_sha(self._text) != self._base_sha

    def set_read_only(self, read_only: bool) -> None:
        self.read_only = read_only

    def set_change_listener(self, listener: ChangeListener | None) -> None:
        self._listener = listener

    def track_line_endings(self, endings: tuple[str, ...] | None) -> None:
        return

    def line_endings(self) -> list[str | None] | None:
        return None

    def edit(self, position: int, remove: int, insert: str) -> None:
        """Simulate a user edit."""
        if self.read_only:
            return
        position = max(0, min(position, len(self._text)))
        self._text = self._text[:position] + insert + self._text[position + remove :]
        self.cursor = position + len(insert)
        if self._listener is not None:
            self._listener()
