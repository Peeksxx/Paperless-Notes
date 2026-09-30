"""The tag grammar shared by tag extraction (``mdio.tags``), the search index and note search.

A tag is ``#`` directly followed by a Unicode letter or digit, then letters, digits, underscore or
hyphen, with at least one letter (so ``#123`` stays an issue number). The ``#`` must not follow a letter,
digit, underscore, ``&``, ``#``, ``/`` or backslash. Tags match without case (``casefold`` of the NFC
form); the first spelling in a note is its display spelling.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

MAX_TAG_CHARS = 64
MAX_TAGS_PER_NOTE = 256
TAG_PATTERN = re.compile(r"(?<![\w&#/\\])#([^\W_][\w-]*)")


@dataclass(frozen=True, slots=True)
class Tag:
    key: str
    display: str


def tag_key(name: str) -> str:
    """Comparison key of a tag name (without the ``#``)."""
    return unicodedata.normalize("NFC", name).casefold()


def acceptable(name: str) -> bool:
    """Length and letter rules for a name the pattern already matched."""
    return len(name) <= MAX_TAG_CHARS and any(ch.isalpha() for ch in name)


def valid_tag_name(name: str) -> bool:
    return TAG_PATTERN.fullmatch("#" + name) is not None and acceptable(name)
