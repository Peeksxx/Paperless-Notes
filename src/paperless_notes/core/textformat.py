"""Bytes on disk <-> editor text, preserving the file's own conventions.

The editor works on text whose line breaks are all ``\\n``. The original BOM and each line's ending
(LF, CRLF or a lone CR) are remembered and restored on save, so an untouched line is written back
byte for byte even in a file with mixed endings.
"""

from __future__ import annotations

import codecs
import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum

DEFAULT_MAX_BYTES = 10 * 1024 * 1024
_BINARY_SNIFF = 8192
_LINE_BREAK = re.compile(r"\r\n|\r|\n")
_UNREPRESENTABLE = re.compile(r"[\u2029\ufdd0-\ufdef\ufffe\uffff]")


class DecodeProblem(Enum):
    TOO_LARGE = "too_large"
    BINARY = "binary"
    NOT_UTF8 = "not_utf8"
    UNREPRESENTABLE = "unrepresentable"


class DecodeError(ValueError):
    def __init__(self, problem: DecodeProblem, detail: str = "") -> None:
        super().__init__(f"{problem.value}{': ' + detail if detail else ''}")
        self.problem = problem


@dataclass(frozen=True, slots=True)
class TextFormat:
    """How a file stores its text. ``endings`` lists every line break in order, for mixed files only."""

    bom: bool = False
    newline: str = "\n"
    endings: tuple[str, ...] | None = None

    @property
    def mixed(self) -> bool:
        return self.endings is not None


NEW_FILE_FORMAT = TextFormat()


def looks_binary(data: bytes) -> bool:
    head = data[:_BINARY_SNIFF]
    if head.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return True
    return b"\x00" in head


def decode(data: bytes, max_bytes: int = DEFAULT_MAX_BYTES) -> tuple[str, TextFormat]:
    """Decode strictly. Raises :class:`DecodeError` instead of guessing."""
    if len(data) > max_bytes:
        raise DecodeError(DecodeProblem.TOO_LARGE, f"{len(data)} bytes")
    if looks_binary(data):
        raise DecodeError(DecodeProblem.BINARY)
    bom = data.startswith(codecs.BOM_UTF8)
    try:
        raw = data[len(codecs.BOM_UTF8) :].decode("utf-8") if bom else data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DecodeError(DecodeProblem.NOT_UTF8, f"invalid byte at offset {exc.start}") from None
    if _UNREPRESENTABLE.search(raw):
        raise DecodeError(DecodeProblem.UNREPRESENTABLE, "paragraph separator or noncharacter")
    if "\r" not in raw:
        return raw, TextFormat(bom=bom)
    crlf = raw.count("\r\n")
    if crlf == raw.count("\r") == raw.count("\n"):
        return raw.replace("\r\n", "\n"), TextFormat(bom=bom, newline="\r\n")
    if "\n" not in raw:
        return raw.replace("\r", "\n"), TextFormat(bom=bom, newline="\r")
    endings = tuple(m.group() for m in _LINE_BREAK.finditer(raw))
    return _LINE_BREAK.sub("\n", raw), TextFormat(bom=bom, newline=_dominant(endings), endings=endings)


def decode_lossy(data: bytes) -> str:
    """Best-effort text for read-only display of files that :func:`decode` refuses."""
    body = data[len(codecs.BOM_UTF8) :] if data.startswith(codecs.BOM_UTF8) else data
    return _LINE_BREAK.sub(
        "\n", body.decode("utf-8", errors="replace").replace("\x00", "\N{REPLACEMENT CHARACTER}")
    )


def _dominant(endings: tuple[str, ...]) -> str:
    crlf = endings.count("\r\n")
    lf = endings.count("\n")
    cr = len(endings) - crlf - lf
    if crlf >= lf and crlf >= cr:
        return "\r\n"
    return "\n" if lf >= cr else "\r"


def encode(
    text: str,
    fmt: TextFormat,
    base_text: str | None = None,
    endings: Sequence[str | None] | None = None,
) -> bytes:
    """Encode ``text`` using ``fmt``.

    For mixed endings the editor's tracked ``endings`` (one per line break, None for new breaks) are
    exact; without them ``base_text`` (the text ``fmt`` was read with) is used to match lines by content.
    """
    lines = text.split("\n")
    breaks = len(lines) - 1
    if not fmt.mixed or (base_text is None and endings is None):
        body = fmt.newline.join(lines)
    else:
        if endings is not None and len(endings) == breaks:
            chosen = [e if e is not None else fmt.newline for e in endings]
        else:
            chosen = _map_endings((base_text or "").split("\n"), fmt.endings or (), lines, fmt.newline)
        parts: list[str] = []
        for i, line in enumerate(lines):
            parts.append(line)
            if i < breaks:
                parts.append(chosen[i])
        body = "".join(parts)
    encoded = body.encode("utf-8")
    return codecs.BOM_UTF8 + encoded if fmt.bom else encoded


def _map_endings(old: list[str], old_endings: tuple[str, ...], new: list[str], default: str) -> list[str]:
    """Line-break string for each line of ``new`` that is followed by a break."""
    result = [default] * max(len(new) - 1, 0)
    prefix = 0
    limit = min(len(old), len(new))
    while prefix < limit and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while suffix < limit - prefix and old[-1 - suffix] == new[-1 - suffix]:
        suffix += 1
    for i in range(min(prefix, len(result), len(old_endings))):
        result[i] = old_endings[i]
    for j in range(suffix):
        new_i = len(new) - 1 - j
        old_i = len(old) - 1 - j
        if 0 <= new_i < len(result) and 0 <= old_i < len(old_endings):
            result[new_i] = old_endings[old_i]
    if prefix + suffix < limit or len(old) != len(new):
        _map_middle(old, old_endings, new, result, prefix, suffix)
    return result


def _map_middle(
    old: list[str],
    old_endings: tuple[str, ...],
    new: list[str],
    result: list[str],
    prefix: int,
    suffix: int,
) -> None:
    import difflib

    old_mid = old[prefix : len(old) - suffix]
    new_mid = new[prefix : len(new) - suffix]
    matcher = difflib.SequenceMatcher(None, old_mid, new_mid, autojunk=False)
    for block in matcher.get_matching_blocks():
        for j in range(block.size):
            old_i = prefix + block.a + j
            new_i = prefix + block.b + j
            if new_i < len(result) and old_i < len(old_endings):
                result[new_i] = old_endings[old_i]
