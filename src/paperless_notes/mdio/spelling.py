"""Spell checking without the operating system: a bundled English word list (US, British, Canadian and
Australian spellings) with three frequency tiers, a personal dictionary, the words of a line worth checking,
and suggestions by edit distance.

A word is known when it is in the list, when it is a capitalized form of a listed lowercase word (the start of
a sentence), when it is a known word followed by 's, or when the user added it. Words with digits or
underscores, all-capital words, words with capitals inside (CamelCase), single letters, and chunks that look
like addresses, paths, tags, mentions or emoji shortcodes are never checked.
"""

from __future__ import annotations

import gzip
import re
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

from paperless_notes.core.jsonstore import read_json, write_json

WORDS_FILE = Path(__file__).resolve().parents[1] / "data" / "words.txt.gz"
MAX_PERSONAL = 20_000
MAX_WORD = 40
_APOSTROPHES = str.maketrans({"\N{RIGHT SINGLE QUOTATION MARK}": "'"})
_ALPHABET = "abcdefghijklmnopqrstuvwxyz"
_CHUNK = re.compile(r"\S+")
_WORD = re.compile(r"[A-Za-z](?:[A-Za-z'\N{RIGHT SINGLE QUOTATION MARK}]*[A-Za-z])?")
_SKIP_CHUNK = re.compile(r"[\d_/\\@]|://|^www\.|^#|[A-Za-z]\.[A-Za-z]|^:[\w+-]+:?$")
_ROWS = ("qwertyuiop", "asdfghjkl", "zxcvbnm")


def _neighbours() -> dict[str, str]:
    near: dict[str, str] = {}
    for r, row in enumerate(_ROWS):
        for c, key in enumerate(row):
            around = [row[i] for i in (c - 1, c + 1) if 0 <= i < len(row)]
            for other in (r - 1, r + 1):
                if 0 <= other < len(_ROWS):
                    around += [_ROWS[other][i] for i in (c - 1, c, c + 1) if 0 <= i < len(_ROWS[other])]
            near[key] = "".join(around)
    return near


_NEAR = _neighbours()


def normalize(word: str) -> str:
    return word.translate(_APOSTROPHES)


def words_to_check(text: str, skip: Iterable[tuple[int, int]] = ()) -> Iterator[tuple[int, int, str]]:
    """(start, end, word) for every word of ``text`` worth checking, outside the ``skip`` ranges."""
    ranges = sorted(skip)
    for chunk in _CHUNK.finditer(text):
        if _SKIP_CHUNK.search(chunk.group(0)):
            continue
        for m in _WORD.finditer(chunk.group(0)):
            start, end = chunk.start() + m.start(), chunk.start() + m.end()
            word = m.group(0)
            if len(word) < 2 or len(word) > MAX_WORD or word.isupper():
                continue
            if any(ch.isupper() for ch in word[1:]):
                continue
            if any(a < end and start < b for a, b in ranges):
                continue
            yield start, end, word


@dataclass
class Speller:
    """The word list plus the personal dictionary; answers whether a word is known and suggests others."""

    tiers: dict[str, int]
    proper: dict[str, tuple[str, int]] = field(default_factory=dict)
    personal: set[str] = field(default_factory=set)
    ignored: set[str] = field(default_factory=set)

    @classmethod
    def from_lines(cls, lines: Iterable[str]) -> Speller:
        """From "<tier><word>" lines, as in the bundled file."""
        tiers: dict[str, int] = {}
        proper: dict[str, tuple[str, int]] = {}
        for line in lines:
            if len(line) < 2 or not line[0].isdigit():
                continue
            tier, word = int(line[0]), normalize(line[1:].strip())
            tiers[word] = tier
            lower = word.lower()
            if word != lower and lower not in proper:
                proper[lower] = (word, tier)
        return cls(tiers, proper)

    @classmethod
    def bundled(cls, path: Path = WORDS_FILE) -> Speller:
        return cls.from_lines(gzip.decompress(path.read_bytes()).decode("utf-8").splitlines())

    def known(self, word: str) -> bool:
        w = normalize(word)
        lower = w.lower()
        if w in self.tiers or lower in self.personal or lower in self.ignored:
            return True
        if w[0].isupper() and w[1:] == w[1:].lower() and lower in self.tiers:
            return True
        return w.endswith("'s") and len(w) > 3 and self.known(w[:-2])

    def misspelled(self, text: str, skip: Iterable[tuple[int, int]] = ()) -> list[tuple[int, int]]:
        return [(start, end) for start, end, word in words_to_check(text, skip) if not self.known(word)]

    def add(self, word: str) -> None:
        if len(self.personal) < MAX_PERSONAL:
            self.personal.add(normalize(word).lower())

    def ignore(self, word: str) -> None:
        self.ignored.add(normalize(word).lower())

    def _lookup(self, candidate: str) -> tuple[str, int] | None:
        if candidate in self.tiers:
            return candidate, self.tiers[candidate]
        return self.proper.get(candidate)

    def _ranked(self, word: str, budget_s: float) -> list[tuple[tuple[int, int, int, int, int], str]]:
        """Known words near ``word`` with their sort keys, best first. Two-edit candidates are searched only
        when nothing is one edit away, within ``budget_s``."""
        lower = normalize(word).lower()
        best: dict[str, tuple[int, int, int, int, int]] = {}

        def consider(candidate: str, distance: int, weight: int) -> None:
            found = self._lookup(candidate)
            if found is None or (found[0].isupper() and len(found[0]) > 1):
                return
            form, tier = found
            key = (distance, weight, tier, int(form[0].lower() != lower[0]), abs(len(form) - len(lower)))
            if form not in best or key < best[form]:
                best[form] = key

        consider(lower, 0, 0)
        first = list(_edits(lower))
        for candidate, weight in first:
            consider(candidate, 1, weight)
        if not best and len(lower) <= 14:
            deadline = time.monotonic() + budget_s
            for count, (step, weight) in enumerate(first):
                if count % 8 == 0 and time.monotonic() > deadline:
                    break
                for candidate, more in _edits(step):
                    if candidate not in best:
                        consider(candidate, 2, weight + more)
        return sorted(((key, form) for form, key in best.items()), key=lambda item: (item[0], item[1]))

    def check(self, word: str, limit: int = 5, budget_s: float = 0.03) -> tuple[str | None, list[str]]:
        """(the one clear correction or None, up to ``limit`` suggestions best first), in the word's
        capitalization. A correction is clear when no other suggestion ranks equal with the first."""
        ranked = self._ranked(word, budget_s)
        suggestions = [match_case(word, form) for _key, form in ranked[:limit]]
        clear = bool(ranked) and (len(ranked) == 1 or ranked[0][0][:3] != ranked[1][0][:3])
        return (suggestions[0] if clear else None), suggestions

    def suggest(self, word: str, limit: int = 5, budget_s: float = 0.03) -> list[str]:
        return self.check(word, limit, budget_s)[1]

    def best(self, word: str) -> str | None:
        return self.check(word)[0]


def match_case(original: str, suggestion: str) -> str:
    if suggestion != suggestion.lower():
        return suggestion
    if original[:1].isupper():
        return suggestion[:1].upper() + suggestion[1:]
    return suggestion


def _edits(word: str) -> Iterator[tuple[str, int]]:
    """Every string one edit from ``word`` with a weight: 0 for swapped neighbours and doubled letters, 1 for
    a neighbouring key, a missing or an extra letter, 2 for any other substitution."""
    for i in range(len(word) + 1):
        head, tail = word[:i], word[i:]
        if tail:
            yield head + tail[1:], 1
        if len(tail) > 1 and tail[0] != tail[1]:
            yield head + tail[1] + tail[0] + tail[2:], 0
        for c in _ALPHABET:
            if tail and c != tail[0]:
                yield head + c + tail[1:], 1 if c in _NEAR.get(tail[0], "") else 2
            doubled = (tail and c == tail[0]) or (head and c == head[-1])
            yield head + c + tail, 0 if doubled else 1


def load_personal(path: Path) -> set[str]:
    data = read_json(path) or {}
    words = data.get("words", [])
    if not isinstance(words, list):
        return set()
    return {
        normalize(w).lower() for w in words[:MAX_PERSONAL] if isinstance(w, str) and 0 < len(w) <= MAX_WORD
    }


def save_personal(path: Path, words: set[str]) -> None:
    write_json(path, {"words": sorted(words)[:MAX_PERSONAL]})
