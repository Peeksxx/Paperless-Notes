"""Images copied into a note's folder.

Layout: ``<note folder>/assets/<name>-<first 12 hex of SHA-256>.<ext>``, referenced from the note as
``assets/<file>``. Names are lower-case ASCII letters, digits and dashes, so they are valid everywhere,
including OneDrive. The content hash makes a repeated paste reuse the same file instead of creating a
copy; a different file never replaces an existing one (creation expects the name to be absent, and a
taken name moves on to a numbered alternative).

Nothing is written until the bytes pass every check: size, a recognised raster format (PNG, JPEG, GIF,
WebP, BMP; never SVG), header dimensions within the pixel budget (so a decompression bomb is refused
without decoding it), and a full decode by the injected decoder. Assets are never deleted
automatically: undo removes the reference only, because another note or a later redo may still need
the file.
"""

from __future__ import annotations

import logging
import ntpath
import re
import struct
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass

from paperless_notes.core import pathid
from paperless_notes.core.fsops import Expect, ExternalChangeError, FileSystem, atomic_save, digest
from paperless_notes.core.security.names import is_device_name

logger = logging.getLogger(__name__)

ASSET_FOLDER = "assets"
MAX_ASSET_BYTES = 20 * 1024 * 1024
MAX_ASSET_PIXELS = 40_000_000
MAX_SIDE = 30_000
MAX_NAME_CHARS = 40
MAX_ALTERNATIVES = 50
EXTENSIONS = {"png": ".png", "jpeg": ".jpg", "gif": ".gif", "webp": ".webp", "bmp": ".bmp"}
SOURCE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"})
UNSUPPORTED = "This is not a supported image. Use PNG, JPEG, GIF, WebP or BMP."
_UNSAFE = re.compile(r"[^a-z0-9_-]+")


@dataclass(frozen=True, slots=True)
class ImageInfo:
    kind: str
    width: int
    height: int


@dataclass(frozen=True, slots=True)
class AssetResult:
    ok: bool
    reference: str | None = None
    path: str | None = None
    problem: str | None = None
    reused: bool = False


def _png(data: bytes) -> ImageInfo | None:
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        width, height = struct.unpack(">II", data[16:24])
        return ImageInfo("png", width, height)
    return None


def _gif(data: bytes) -> ImageInfo | None:
    if len(data) >= 10 and data[:6] in (b"GIF87a", b"GIF89a"):
        width, height = struct.unpack("<HH", data[6:10])
        return ImageInfo("gif", width, height)
    return None


def _bmp(data: bytes) -> ImageInfo | None:
    if len(data) < 26 or data[:2] != b"BM":
        return None
    size = struct.unpack("<I", data[14:18])[0]
    if size == 12:
        width, height = struct.unpack("<HH", data[18:22])
    elif size >= 40:
        width, height = struct.unpack("<ii", data[18:26])
    else:
        return None
    return ImageInfo("bmp", width, abs(height))


def _jpeg(data: bytes) -> ImageInfo | None:
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None
    i = 2
    for _ in range(10_000):
        while i < len(data) and data[i] == 0xFF:
            i += 1
        if i >= len(data):
            return None
        marker = data[i]
        i += 1
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if marker == 0xD9 or i + 2 > len(data):
            return None
        length = struct.unpack(">H", data[i : i + 2])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            if i + 7 > len(data):
                return None
            height, width = struct.unpack(">HH", data[i + 3 : i + 7])
            return ImageInfo("jpeg", width, height)
        if length < 2:
            return None
        i += length
    return None


def _webp(data: bytes) -> ImageInfo | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8 " and data[23:26] == b"\x9d\x01\x2a":
        width, height = struct.unpack("<HH", data[26:30])
        return ImageInfo("webp", width & 0x3FFF, height & 0x3FFF)
    if chunk == b"VP8L" and data[20] == 0x2F:
        b = data[21:25]
        width = 1 + (((b[1] & 0x3F) << 8) | b[0])
        height = 1 + (((b[3] & 0x0F) << 10) | (b[2] << 2) | ((b[1] & 0xC0) >> 6))
        return ImageInfo("webp", width, height)
    if chunk == b"VP8X":
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return ImageInfo("webp", width, height)
    return None


def sniff_image(data: bytes) -> ImageInfo | None:
    """Format and dimensions from the header alone; nothing is decoded."""
    for probe in (_png, _jpeg, _gif, _webp, _bmp):
        info = probe(data)
        if info is not None:
            return info
    return None


def safe_stem(hint: str) -> str:
    """A filename-safe stem from a suggested name; the hint's folder parts are ignored."""
    stem = ntpath.splitext(ntpath.basename(hint.replace("/", "\\")))[0]
    stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii").lower()
    stem = _UNSAFE.sub("-", stem).strip("-_")[:MAX_NAME_CHARS].strip("-_")
    if not stem or is_device_name(stem):
        return "image"
    return stem


class AssetService:
    def __init__(
        self,
        fs: FileSystem,
        decoder: Callable[[bytes, int], bool] | None = None,
        max_bytes: int = MAX_ASSET_BYTES,
        max_pixels: int = MAX_ASSET_PIXELS,
    ) -> None:
        self._fs = fs
        self._decoder = decoder
        self._max_bytes = max_bytes
        self._max_pixels = max_pixels

    def check(self, data: bytes) -> tuple[ImageInfo | None, str | None]:
        """The image's header information, or the reason it is refused."""
        if not data:
            return None, "The image is empty."
        if len(data) > self._max_bytes:
            return None, f"The image is larger than {self._max_bytes // (1024 * 1024)} MB."
        info = sniff_image(data)
        if info is None:
            return None, UNSUPPORTED
        width, height = info.width, info.height
        if (
            width <= 0
            or height <= 0
            or width > MAX_SIDE
            or height > MAX_SIDE
            or width * height > self._max_pixels
        ):
            return None, f"The image is too large to use ({width} x {height} pixels)."
        if self._decoder is not None and not self._decoder(data, self._max_pixels):
            return None, "The image could not be read; it may be damaged."
        return info, None

    def store(self, note_path: str, data: bytes, name_hint: str = "") -> AssetResult:
        """Copy image bytes into the note's assets folder; the reference is relative to the note."""
        info, problem = self.check(data)
        if info is None:
            return AssetResult(False, problem=problem)
        folder = ntpath.join(ntpath.dirname(pathid.normalize(note_path)), ASSET_FOLDER)
        sha = digest(data)
        base = f"{safe_stem(name_hint)}-{sha[:12]}"
        extension = EXTENSIONS[info.kind]
        try:
            self._fs.make_dirs(folder)
        except OSError as exc:
            logger.warning("Creating the assets folder failed: %s", exc)
            return AssetResult(
                False, problem=f"The image could not be saved next to the note: {_reason(exc)}"
            )
        for attempt in range(MAX_ALTERNATIVES):
            name = base + (f"-{attempt + 1}" if attempt else "") + extension
            path = ntpath.join(folder, name)
            existing = self._fs.stat(path)
            if existing is not None:
                if existing.size == len(data) and self._same(path, sha):
                    return AssetResult(True, f"{ASSET_FOLDER}/{name}", path, reused=True)
                continue
            try:
                atomic_save(self._fs, path, data, Expect.ABSENT, self._max_bytes)
            except (FileExistsError, ExternalChangeError):
                continue
            except OSError as exc:
                logger.warning("Saving an image asset failed: %s", exc)
                return AssetResult(
                    False, problem=f"The image could not be saved next to the note: {_reason(exc)}"
                )
            return AssetResult(True, f"{ASSET_FOLDER}/{name}", path)
        return AssetResult(False, problem="There are too many images with this name already.")

    def store_file(self, note_path: str, source: str) -> AssetResult:
        """Copy a local image file; anything that is not a supported image is refused unread."""
        if ntpath.splitext(source)[1].lower() not in SOURCE_EXTENSIONS:
            return AssetResult(False, problem=UNSUPPORTED)
        try:
            data = self._fs.read_bytes(pathid.normalize(source), self._max_bytes)
        except OSError as exc:
            return AssetResult(False, problem=f"The image file could not be read: {_reason(exc)}")
        return self.store(note_path, data, ntpath.basename(source))

    def _same(self, path: str, sha: str) -> bool:
        try:
            return digest(self._fs.read_bytes(path, self._max_bytes)) == sha
        except OSError as exc:
            logger.info("Could not compare an existing asset: %s", exc)
            return False


def _reason(exc: OSError) -> str:
    return str(exc.strerror or exc.__class__.__name__)
