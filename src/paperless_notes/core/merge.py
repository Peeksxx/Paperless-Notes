"""Three-way merge by line, refined word by word inside lines that both sides changed.

Overlapping changes are conflicts; nothing is guessed. Word-level refinement only succeeds when the two
edits touch disjoint token ranges separated by at least one unchanged token, so a paragraph written as one
long line can take edits from two PCs without dropping either. Edit scripts come from the bounded diff, so a
merge never runs in quadratic time; a coarse region only makes a conflict more likely, never a wrong merge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from paperless_notes.core.diffing import DiffLimits, sequence_opcodes

_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
_LIMITS = DiffLimits()


@dataclass(frozen=True, slots=True)
class MergeResult:
    text: str | None
    conflicts: int
    word_merges: int = 0

    @property
    def clean(self) -> bool:
        return self.text is not None


@dataclass(frozen=True, slots=True)
class _Hunk:
    start: int
    end: int
    items: tuple[str, ...]


def _hunks(base: list[str], other: list[str]) -> list[_Hunk]:
    codes, _ = sequence_opcodes(base, other, _LIMITS)
    return [_Hunk(i1, i2, tuple(other[j1:j2])) for tag, i1, i2, j1, j2 in codes if tag != "equal"]


def _overlaps(x: _Hunk, y: _Hunk, strict: bool) -> bool:
    if strict:
        return not (x.end < y.start or y.end < x.start)
    if x.start == x.end or y.start == y.end:
        point, span = (x, y) if x.start == x.end else (y, x)
        return span.start <= point.start <= span.end
    return x.start < y.end and y.start < x.end


def _side_segment(
    base: list[str], group: list[tuple[_Hunk, int]], side: int, start: int, end: int
) -> list[str]:
    out: list[str] = []
    pos = start
    for hunk, owner in sorted(group, key=lambda t: (t[0].start, t[0].end)):
        if owner != side:
            continue
        out.extend(base[pos : hunk.start])
        out.extend(hunk.items)
        pos = hunk.end
    out.extend(base[pos:end])
    return out


class _Merger:
    def __init__(self, strict: bool) -> None:
        self.strict = strict
        self.word_merges = 0

    def merge(self, base: list[str], ours: list[str], theirs: list[str]) -> list[str] | None:
        if ours == theirs or base == theirs:
            return ours
        if base == ours:
            return theirs
        tagged = [(h, 0) for h in _hunks(base, ours)] + [(h, 1) for h in _hunks(base, theirs)]
        tagged.sort(key=lambda t: (t[0].start, t[0].end, t[1]))
        groups: list[list[tuple[_Hunk, int]]] = []
        for hunk, side in tagged:
            touching = [g for g in groups if any(_overlaps(hunk, other, self.strict) for other, _ in g)]
            joined = [(hunk, side)]
            for g in touching:
                joined.extend(g)
                groups.remove(g)
            groups.append(joined)
        chosen: list[_Hunk] = []
        for group in groups:
            if len({side for _, side in group}) == 1:
                chosen.extend(h for h, _ in group)
            elif all(h == group[0][0] for h, _ in group):
                chosen.append(group[0][0])
            else:
                refined = None if self.strict else self._refine(base, group)
                if refined is None:
                    return None
                chosen.append(refined)
        chosen.sort(key=lambda h: (h.start, h.end))
        out: list[str] = []
        pos = 0
        for h in chosen:
            if h.start < pos:
                return None
            out.extend(base[pos : h.start])
            out.extend(h.items)
            pos = h.end
        out.extend(base[pos:])
        return out

    def _refine(self, base: list[str], group: list[tuple[_Hunk, int]]) -> _Hunk | None:
        start = min(h.start for h, _ in group)
        end = max(h.end for h, _ in group)
        ours = _side_segment(base, group, 0, start, end)
        theirs = _side_segment(base, group, 1, start, end)
        if not (len(ours) == len(theirs) == end - start):
            return None
        lines: list[str] = []
        for b_line, o_line, t_line in zip(base[start:end], ours, theirs, strict=True):
            if o_line == t_line or t_line == b_line:
                lines.append(o_line)
            elif o_line == b_line:
                lines.append(t_line)
            else:
                words = merge_words(b_line, o_line, t_line)
                if words is None:
                    return None
                self.word_merges += 1
                lines.append(words)
        return _Hunk(start, end, tuple(lines))


def merge_words(base: str, ours: str, theirs: str) -> str | None:
    """Merge one line at word granularity, or None when the edits touch or overlap."""
    merged = _Merger(strict=True).merge(_TOKEN.findall(base), _TOKEN.findall(ours), _TOKEN.findall(theirs))
    return None if merged is None else "".join(merged)


def merge3(base: str, ours: str, theirs: str) -> MergeResult:
    merger = _Merger(strict=False)
    lines = merger.merge(base.split("\n"), ours.split("\n"), theirs.split("\n"))
    if lines is None:
        return MergeResult(None, 1)
    return MergeResult("\n".join(lines), 0, merger.word_merges)
