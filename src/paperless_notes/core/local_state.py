"""Per-machine state that is not a user setting: soft read-only locks and the install id.

A soft lock never touches the synced file's attributes; it only makes this PC open the note read-only.
"""

from __future__ import annotations

import logging
import re
import secrets
from pathlib import Path

from paperless_notes.core import pathid
from paperless_notes.core.jsonstore import read_json, write_json

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1
MAX_LOCKS = 10_000
MAX_PATH_CHARS = 32_767
MAX_RECENT = 20
NOTE_FONTS = ("sans", "serif", "mono")
_INSTALL_ID = re.compile(r"[0-9a-f]{32}")


class LocalStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._locks: dict[str, str] = {}
        self._recent: list[str] = []
        self._fonts: dict[str, str] = {}
        self._install_id = ""
        self._load()
        if not self._install_id:
            self._install_id = secrets.token_hex(16)
            self._save()

    @property
    def install_id(self) -> str:
        """Random id of this installation, used to tell this PC's revisions apart in the ledger."""
        return self._install_id

    def _load(self) -> None:
        data = read_json(self._path)
        if data is None or data.get("schema_version") != SCHEMA_VERSION:
            return
        install_id = data.get("install_id")
        if isinstance(install_id, str) and _INSTALL_ID.fullmatch(install_id):
            self._install_id = install_id
        recent = data.get("recent_notes")
        if isinstance(recent, list):
            self._recent = [
                p for p in recent[:MAX_RECENT] if isinstance(p, str) and 0 < len(p) <= MAX_PATH_CHARS
            ]
        fonts = data.get("note_fonts")
        if isinstance(fonts, dict):
            for path, font in list(fonts.items())[:MAX_LOCKS]:
                if isinstance(path, str) and 0 < len(path) <= MAX_PATH_CHARS and font in NOTE_FONTS:
                    self._fonts[pathid.identity(path)] = font
        locks = data.get("soft_locks")
        if not isinstance(locks, list):
            return
        for item in locks[:MAX_LOCKS]:
            if isinstance(item, str) and 0 < len(item) <= MAX_PATH_CHARS:
                self._locks[pathid.identity(item)] = item

    def _save(self) -> None:
        write_json(
            self._path,
            {
                "schema_version": SCHEMA_VERSION,
                "install_id": self._install_id,
                "soft_locks": sorted(self._locks.values()),
                "recent_notes": self._recent,
                "note_fonts": dict(sorted(self._fonts.items())),
            },
        )

    def is_locked(self, path: str) -> bool:
        return pathid.identity(path) in self._locks

    def locked_paths(self) -> list[str]:
        return sorted(self._locks.values())

    def recent_notes(self) -> list[str]:
        return list(self._recent)

    def add_recent(self, path: str) -> None:
        key = pathid.identity(path)
        kept = [p for p in self._recent if pathid.identity(p) != key]
        updated = [pathid.normalize(path), *kept][:MAX_RECENT]
        if updated != self._recent:
            self._recent = updated
            self._save()

    def forget_recent(self, path: str) -> None:
        key = pathid.identity(path)
        kept = [p for p in self._recent if pathid.identity(p) != key]
        if kept != self._recent:
            self._recent = kept
            self._save()

    def note_font(self, path: str) -> str | None:
        return self._fonts.get(pathid.identity(path))

    def set_note_font(self, path: str, font: str | None) -> None:
        key = pathid.identity(path)
        if font is None:
            changed = self._fonts.pop(key, None) is not None
        elif font in NOTE_FONTS and self._fonts.get(key) != font:
            self._fonts[key] = font
            changed = True
        else:
            changed = False
        if changed:
            self._save()

    def set_locked(self, path: str, locked: bool) -> None:
        key = pathid.identity(path)
        if locked == (key in self._locks):
            return
        if locked:
            self._locks[key] = pathid.normalize(path)
        else:
            self._locks.pop(key, None)
        self._save()
