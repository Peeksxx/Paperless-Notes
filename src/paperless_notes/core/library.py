"""A set of watched note folders with a change-event stream and a text-extraction hook (F18, F19, F24).

The index lives here only as file metadata. Full-text search (`core.search_index`) stores its index on disk
and uses ``extract_text`` so unreadable notes are skipped consistently.
"""

from __future__ import annotations

import logging
import ntpath
import os
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass

from PySide6.QtCore import QObject, Signal

from paperless_notes.core import pathid, textformat
from paperless_notes.core.fsops import FileSystem
from paperless_notes.core.onedrive import TEMP_PREFIX

logger = logging.getLogger(__name__)

NOTE_EXTENSIONS = (".md", ".markdown", ".txt")
MAX_DEPTH = 32

type TextExtractor = Callable[[str, bytes], str | None]
type Walker = Callable[[str], Iterator[tuple[str, int, int]]]


@dataclass(frozen=True, slots=True)
class NoteEntry:
    path: str
    size: int
    mtime_ns: int


@dataclass(frozen=True, slots=True)
class LibraryEvent:
    kind: str
    path: str


def plain_text_extractor(path: str, data: bytes) -> str | None:
    try:
        return textformat.decode(data)[0]
    except textformat.DecodeError:
        return None


def walk_notes(root: str) -> Iterator[tuple[str, int, int]]:
    """Yield ``(path, size, mtime_ns)`` for notes under ``root``. Never follows links or hydrates files."""
    stack = [(root, 0)]
    while stack:
        folder, depth = stack.pop()
        try:
            with os.scandir(pathid.to_os_path(folder)) as entries:
                for entry in entries:
                    name = entry.name
                    if name.startswith((".", TEMP_PREFIX)):
                        continue
                    full = ntpath.join(folder, name)
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            if depth < MAX_DEPTH:
                                stack.append((full, depth + 1))
                        elif name.casefold().endswith(NOTE_EXTENSIONS) and entry.is_file(
                            follow_symlinks=False
                        ):
                            st = entry.stat(follow_symlinks=False)
                            yield full, st.st_size, st.st_mtime_ns
                    except OSError as exc:
                        logger.debug("Skipping %s: %s", name, exc)
        except OSError as exc:
            logger.warning("Cannot list a library folder: %s", exc)


class Library(QObject):
    changed = Signal(list)

    def __init__(
        self,
        fs: FileSystem,
        roots: Sequence[str] = (),
        extractor: TextExtractor = plain_text_extractor,
        walker: Walker = walk_notes,
        max_bytes: int = textformat.DEFAULT_MAX_BYTES,
    ) -> None:
        super().__init__()
        self._fs = fs
        self._extractor = extractor
        self._walker = walker
        self._max_bytes = max_bytes
        self._roots: list[str] = []
        self._index: dict[str, NoteEntry] = {}
        for root in roots:
            self.add_root(root)

    def roots(self) -> list[str]:
        return list(self._roots)

    def add_root(self, root: str) -> bool:
        normalized = pathid.normalize(root)
        if any(pathid.same_path(normalized, r) for r in self._roots):
            return False
        self._roots.append(normalized)
        return True

    def remove_root(self, root: str) -> bool:
        before = len(self._roots)
        self._roots = [r for r in self._roots if not pathid.same_path(r, root)]
        return len(self._roots) != before

    def notes(self) -> list[NoteEntry]:
        return list(self._index.values())

    def scan(self) -> list[LibraryEvent]:
        fresh: dict[str, NoteEntry] = {}
        for root in self._roots:
            for path, size, mtime in self._walker(root):
                fresh.setdefault(pathid.identity(path), NoteEntry(path, size, mtime))
        events = [LibraryEvent("removed", e.path) for k, e in self._index.items() if k not in fresh]
        for key, entry in fresh.items():
            old = self._index.get(key)
            if old is None:
                events.append(LibraryEvent("added", entry.path))
            elif (old.size, old.mtime_ns) != (entry.size, entry.mtime_ns):
                events.append(LibraryEvent("modified", entry.path))
        self._index = fresh
        if events:
            self.changed.emit(events)
        return events

    def extract_text(self, path: str) -> str | None:
        try:
            data = self._fs.read_bytes(path, self._max_bytes)
        except OSError as exc:
            logger.info("Cannot read a note for indexing: %s", exc)
            return None
        return self._extractor(path, data)
