"""Crash-safety journal of unsaved text, one small file per note in the local state folder."""

from __future__ import annotations

import json
import logging
import ntpath
from dataclasses import dataclass

from paperless_notes.core import pathid
from paperless_notes.core.editor import text_sha
from paperless_notes.core.fsops import Expect, FileSystem, atomic_save

logger = logging.getLogger(__name__)

DRAFT_SUFFIX = ".draft"
_FORMAT_VERSION = 1
_MAX_DRAFT_BYTES = 64 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class Draft:
    path: str
    text: str
    base_sha: str | None
    saved_at: float


class DraftStore:
    def __init__(self, fs: FileSystem, directory: str) -> None:
        self._fs = fs
        self._dir = directory

    def file_for(self, path: str) -> str:
        return ntpath.join(self._dir, pathid.path_key(path)[:32] + DRAFT_SUFFIX)

    def write(self, path: str, text: str, base_sha: str | None, saved_at: float) -> None:
        header = {
            "v": _FORMAT_VERSION,
            "path": pathid.normalize(path),
            "base_sha": base_sha,
            "saved_at": saved_at,
            "sha": text_sha(text),
        }
        data = json.dumps(header, ensure_ascii=True).encode("ascii") + b"\n" + text.encode("utf-8")
        self._fs.make_dirs(self._dir)
        atomic_save(self._fs, self.file_for(path), data, Expect.ANY, limit=len(data) + 1)

    def delete(self, path: str) -> None:
        self._fs.remove(self.file_for(path))

    def load(self, path: str) -> Draft | None:
        draft = self._read(self.file_for(path))
        if draft is not None and not pathid.same_path(draft.path, path):
            return None
        return draft

    def list_all(self) -> list[Draft]:
        try:
            names = self._fs.list_dir(self._dir)
        except FileNotFoundError:
            return []
        drafts = [self._read(ntpath.join(self._dir, n)) for n in names if n.endswith(DRAFT_SUFFIX)]
        return [d for d in drafts if d is not None]

    def _read(self, file: str) -> Draft | None:
        try:
            data = self._fs.read_bytes(file, _MAX_DRAFT_BYTES)
        except FileNotFoundError:
            return None
        head, sep, body = data.partition(b"\n")
        try:
            header = json.loads(head.decode("ascii"))
            text = body.decode("utf-8")
            valid = (
                sep == b"\n"
                and isinstance(header, dict)
                and header.get("v") == _FORMAT_VERSION
                and isinstance(header.get("path"), str)
                and header.get("sha") == text_sha(text)
            )
        except (UnicodeDecodeError, ValueError):
            valid = False
        if not valid:
            logger.warning("Ignoring unreadable draft %s", ntpath.basename(file))
            return None
        base_sha = header.get("base_sha")
        saved_at = header.get("saved_at")
        return Draft(
            path=header["path"],
            text=text,
            base_sha=base_sha if isinstance(base_sha, str) else None,
            saved_at=float(saved_at) if isinstance(saved_at, int | float) else 0.0,
        )
