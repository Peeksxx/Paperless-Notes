"""Path identity on Windows: case-insensitive, NFC-normalised and long-path aware."""

from __future__ import annotations

import hashlib
import ntpath
import os
import re
import unicodedata

LONG_PREFIX = "\\\\?\\"
UNC_LONG_PREFIX = "\\\\?\\UNC\\"
_MAX_PLAIN_PATH = 240
_UNSAFE_NAME_CHARS = re.compile(r"[^\w.-]+")

type StrPath = str | os.PathLike[str]


def strip_long_prefix(path: str) -> str:
    if path.startswith(UNC_LONG_PREFIX):
        return "\\\\" + path[len(UNC_LONG_PREFIX) :]
    if path.startswith(LONG_PREFIX):
        return path[len(LONG_PREFIX) :]
    return path


def normalize(path: StrPath) -> str:
    """Absolute, normalised path without the long-path prefix."""
    return ntpath.normpath(ntpath.abspath(strip_long_prefix(os.fspath(path))))


def identity(path: StrPath) -> str:
    """Comparison key: two paths with the same identity refer to the same note."""
    return unicodedata.normalize("NFC", normalize(path)).casefold()


def path_key(path: StrPath) -> str:
    return hashlib.sha256(identity(path).encode("utf-8")).hexdigest()


def same_path(a: StrPath, b: StrPath) -> bool:
    return identity(a) == identity(b)


def is_within(child: StrPath, parent: StrPath) -> bool:
    c = identity(child)
    p = identity(parent).rstrip("\\")
    return c == p or c.startswith(p + "\\")


def to_os_path(path: StrPath) -> str:
    """Path usable by Win32 APIs even beyond MAX_PATH."""
    p = normalize(path)
    if len(p) < _MAX_PLAIN_PATH:
        return p
    if p.startswith("\\\\"):
        return UNC_LONG_PREFIX + p[2:]
    return LONG_PREFIX + p


def readable_name(path: StrPath, limit: int = 40) -> str:
    stem = ntpath.splitext(ntpath.basename(normalize(path)))[0]
    cleaned = _UNSAFE_NAME_CHARS.sub("_", unicodedata.normalize("NFC", stem)).strip("._")
    return (cleaned or "note")[:limit]
