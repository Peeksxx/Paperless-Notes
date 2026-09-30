"""Bounded store of note versions that sync diagnoses compare, kept apart from optional history.

A diagnosis names versions by content hash. So that its comparison still works after a restart, or when
the user switched history off, the monitor keeps recent versions of each open note here:
``<directory>\\<path key>\\<sha256>.z`` (zlib-compressed text). Everything stays on this PC in the app data
folder, like drafts and history.

Limits, enforced on every write and whenever a note is released (closed), not only at startup:
- per note, the newest ``per_note`` versions plus any the caller asks to keep;
- in total, ``max_total_bytes``: the oldest evidence is removed first until the total fits, except versions
  an active note has protected (the exact hashes its open diagnoses and ledger head need). If protected
  versions alone exceed the total, the store exceeds it by exactly those and holds nothing else; they become
  removable as soon as their note releases them. Unprotected growth is therefore always bounded.
"""

from __future__ import annotations

import logging
import ntpath
import re
import threading
import zlib
from collections.abc import Hashable, Iterable
from dataclasses import dataclass

from paperless_notes.core import pathid
from paperless_notes.core.fsops import Expect, FileSystem, atomic_save

logger = logging.getLogger(__name__)

PER_NOTE = 4
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_BLOB_BYTES = 8 * 1024 * 1024
_SHA = re.compile(r"[0-9a-f]{64}")
_SUFFIX = ".z"


@dataclass(slots=True)
class _Entry:
    path: str
    folder: str
    sha: str
    mtime_ns: int
    order: int
    size: int


class EvidenceStore:
    def __init__(
        self,
        fs: FileSystem,
        directory: str,
        per_note: int = PER_NOTE,
        max_total_bytes: int = MAX_TOTAL_BYTES,
    ) -> None:
        self._fs = fs
        self._dir = directory
        self._per_note = per_note
        self._max_total = max_total_bytes
        self._lock = threading.Lock()
        self._index: dict[str, _Entry] = {}
        self._indexed = False
        self._total = 0
        self._counter = 0
        self._protected: dict[str, dict[Hashable, frozenset[str]]] = {}

    def folder_for(self, path: str) -> str:
        return ntpath.join(self._dir, pathid.path_key(path)[:32])

    @property
    def total_bytes(self) -> int:
        with self._lock:
            self._ensure_index()
            return self._total

    def put(
        self,
        path: str,
        sha: str,
        text: str,
        keep: Iterable[str] = (),
        *,
        owner: Hashable | None = None,
        protect: Iterable[str] = (),
    ) -> None:
        """Store one version and optionally replace one owner's protection in the same eviction lock."""
        if not _SHA.fullmatch(sha):
            return
        data = zlib.compress(text.encode("utf-8", "surrogatepass"), 6)
        if len(data) > MAX_BLOB_BYTES:
            logger.info("Version too large to keep as sync evidence")
            return
        folder = self.folder_for(path)
        target = ntpath.join(folder, sha + _SUFFIX)
        with self._lock:
            if owner is not None:
                owners = self._protected.setdefault(folder.casefold(), {})
                owners[owner] = frozenset(protect)
            index = self._ensure_index()
            if target.casefold() not in index:
                self._fs.make_dirs(folder)
                atomic_save(self._fs, target, data, Expect.ANY, len(data) + 1)
                st = self._fs.stat(target)
                self._counter += 1
                size = st.size if st is not None else len(data)
                mtime = st.mtime_ns if st is not None else 0
                self._add(_Entry(target, folder.casefold(), sha, mtime, self._counter, size))
            self._prune_note(folder.casefold(), {sha, *keep})
            self._evict()

    def protect(self, path: str, owner: Hashable, hashes: Iterable[str]) -> None:
        """Declare the exact versions ``owner`` (an open note's monitor) needs; replaces its earlier set."""
        with self._lock:
            owners = self._protected.setdefault(self.folder_for(path).casefold(), {})
            owners[owner] = frozenset(hashes)

    def release(self, path: str, owner: Hashable) -> None:
        """The note closed: its versions become removable, and the limits are applied right away."""
        folder = self.folder_for(path).casefold()
        with self._lock:
            owners = self._protected.get(folder)
            if owners is not None:
                owners.pop(owner, None)
                if not owners:
                    del self._protected[folder]
            self._ensure_index()
            self._evict()

    def get(self, path: str, sha: str) -> str | None:
        if not _SHA.fullmatch(sha):
            return None
        target = ntpath.join(self.folder_for(path), sha + _SUFFIX)
        try:
            blob = self._fs.read_bytes(target, MAX_BLOB_BYTES)
        except FileNotFoundError:
            return None
        except OSError as exc:
            logger.info("Sync evidence unreadable: %s", exc)
            return None
        try:
            return zlib.decompress(blob).decode("utf-8", "surrogatepass")
        except (zlib.error, UnicodeDecodeError) as exc:
            logger.warning("Sync evidence damaged, ignored: %s", exc)
            return None

    def clear(self, path: str) -> None:
        """Remove every kept version of one note (for "clear history")."""
        folder = self.folder_for(path).casefold()
        with self._lock:
            index = self._ensure_index()
            for key in [k for k, e in index.items() if e.folder == folder]:
                self._remove(key)

    def clear_all(self) -> int:
        """Remove every kept version of every note (for "clear all history"). Note files are untouched."""
        with self._lock:
            index = self._ensure_index()
            removed = sum(self._remove(key) for key in list(index))
            try:
                folders = self._fs.list_dir(self._dir)
            except FileNotFoundError:
                folders = []
            for name in folders:
                folder = ntpath.join(self._dir, name)
                try:
                    stray = [n for n in self._fs.list_dir(folder) if n.endswith(_SUFFIX)]
                except (FileNotFoundError, NotADirectoryError):
                    continue
                for file in stray:
                    self._fs.remove(ntpath.join(folder, file))
                    removed += 1
            return removed

    def enforce_caps(self, open_paths: Iterable[str] = ()) -> int:
        """Apply the total limit now, treating every version of ``open_paths`` as protected."""
        spared = frozenset(self.folder_for(p).casefold() for p in open_paths)
        with self._lock:
            self._ensure_index()
            return self._evict(spared)

    def _ensure_index(self) -> dict[str, _Entry]:
        if not self._indexed:
            self._indexed = True
            self._index.clear()
            self._total = 0
            try:
                folders = self._fs.list_dir(self._dir)
            except FileNotFoundError:
                folders = []
            for name in folders:
                folder = ntpath.join(self._dir, name)
                try:
                    files = [n for n in self._fs.list_dir(folder) if n.endswith(_SUFFIX)]
                except (FileNotFoundError, NotADirectoryError):
                    continue
                for file in files:
                    full = ntpath.join(folder, file)
                    st = self._fs.stat(full)
                    if st is not None:
                        sha = file[: -len(_SUFFIX)]
                        self._add(_Entry(full, folder.casefold(), sha, st.mtime_ns, 0, st.size))
        return self._index

    def _add(self, entry: _Entry) -> None:
        self._index[entry.path.casefold()] = entry
        self._total += entry.size

    def _remove(self, key: str) -> bool:
        entry = self._index[key]
        try:
            self._fs.remove(entry.path)
        except OSError as exc:
            logger.warning("Could not remove old sync evidence: %s", exc)
            return False
        del self._index[key]
        self._total -= entry.size
        return True

    def _is_protected(self, entry: _Entry, spared: frozenset[str] = frozenset()) -> bool:
        if entry.folder in spared:
            return True
        owners = self._protected.get(entry.folder)
        return owners is not None and any(entry.sha in hashes for hashes in owners.values())

    def _prune_note(self, folder: str, keep: set[str]) -> None:
        """Newest first by modification time; versions written in the same tick keep their write order."""
        entries = sorted(
            ((k, e) for k, e in self._index.items() if e.folder == folder),
            key=lambda item: (item[1].mtime_ns, item[1].order),
            reverse=True,
        )
        for rank, (key, entry) in enumerate(entries):
            if rank >= self._per_note and entry.sha not in keep and not self._is_protected(entry):
                self._remove(key)

    def _evict(self, spared: frozenset[str] = frozenset()) -> int:
        if self._total <= self._max_total:
            return 0
        candidates = sorted(
            ((k, e) for k, e in self._index.items() if not self._is_protected(e, spared)),
            key=lambda item: (item[1].mtime_ns, item[1].order),
        )
        removed = 0
        for key, _ in candidates:
            if self._total <= self._max_total:
                break
            removed += self._remove(key)
        if self._total > self._max_total:
            logger.info("Sync evidence is above its cap only by versions that open notes still need")
        return removed
