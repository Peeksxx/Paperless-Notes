"""Which images a note may display (SEC1).

Only local raster images inside the note's own folder tree are allowed. Anything with a scheme
(``file:``, ``http:``, ``data:``, a drive letter), anything rooted or UNC, ``..``, alternate data
streams, device names and odd trailing characters are refused before the file system is touched.
"""

from __future__ import annotations

import ntpath
import os
import re
from dataclasses import dataclass

from paperless_notes.core import pathid
from paperless_notes.core.fsops import FileSystem
from paperless_notes.core.onedrive import needs_hydration
from paperless_notes.core.security.names import has_control_chars, is_device_name

IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
MAX_REFERENCE_CHARS = 2048
_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")
_PERCENT = re.compile(r"%([0-9A-Fa-f]{2})")


def _percent_decode(text: str) -> str | None:
    if "%" not in text:
        return text
    raw = bytearray()
    i = 0
    while i < len(text):
        m = _PERCENT.match(text, i)
        if m:
            raw.append(int(m.group(1), 16))
            i = m.end()
        else:
            raw.extend(text[i].encode("utf-8"))
            i += 1
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return None


@dataclass(frozen=True, slots=True)
class ResourcePolicy:
    note_dir: str
    max_bytes: int = 20 * 1024 * 1024
    max_pixels: int = 40_000_000

    def resolve(self, reference: str) -> str | None:
        """Canonical path of an allowed image, or None."""
        ref = reference.strip()
        if not ref or len(ref) > MAX_REFERENCE_CHARS or has_control_chars(ref):
            return None
        decoded = _percent_decode(ref)
        if decoded is None or has_control_chars(decoded) or any(c in decoded for c in '?#*<>|"'):
            return None
        if _SCHEME.match(decoded) or decoded.startswith(("/", "\\")):
            return None
        parts = [p for p in re.split(r"[\\/]+", decoded) if p not in ("", ".")]
        if not parts:
            return None
        for part in parts:
            if part == ".." or ":" in part or part != part.rstrip(". ") or is_device_name(part):
                return None
        if ntpath.splitext(parts[-1])[1].lower() not in IMAGE_EXTENSIONS:
            return None
        root = os.path.realpath(pathid.normalize(self.note_dir))
        real = os.path.realpath(ntpath.join(root, *parts))
        if not pathid.is_within(real, root) or pathid.same_path(real, root):
            return None
        return real

    def load(self, reference: str, fs: FileSystem) -> bytes | None:
        """Bytes of an allowed image, never blocking on a cloud-only file."""
        path = self.resolve(reference)
        if path is None:
            return None
        st = fs.stat(path)
        if st is None or st.size > self.max_bytes or needs_hydration(st.attributes):
            return None
        try:
            return fs.read_bytes(path, self.max_bytes)
        except OSError:
            return None
