"""Bounded diffs for explaining divergences. Diff content is shown to the user, never logged.

The common prefix and suffix are trimmed first; exact matching (``difflib``) runs only while the changed
middle stays under ``exact_max_lines``; beyond that the middle is one coarse hunk, so time and memory stay
bounded for multi-megabyte inputs.
"""

from __future__ import annotations

import bisect
import difflib
import re
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum

_TOKEN = re.compile(r"\w+|\s+|[^\w\s]")
_BREAK = re.compile(r"\r\n|\r|\n")
_SMALL_REGION_CELLS = 250_000
_MAX_DEPTH = 48


class Op(Enum):
    EQUAL = "equal"
    DELETE = "delete"
    INSERT = "insert"


@dataclass(frozen=True, slots=True)
class DiffLimits:
    exact_max_lines: int = 400_000
    work_budget: int = 4_000_000
    context: int = 3
    max_hunks: int = 200
    max_block_lines: int = 400
    word_max_chars: int = 4_000


@dataclass(frozen=True, slots=True)
class DiffLine:
    op: Op
    text: str
    left_no: int | None
    right_no: int | None


@dataclass(frozen=True, slots=True)
class Hunk:
    left_start: int
    left_count: int
    right_start: int
    right_count: int
    lines: tuple[DiffLine, ...]
    coarse: bool = False


@dataclass(frozen=True, slots=True)
class DiffStats:
    added: int
    removed: int
    hunks: int


@dataclass(frozen=True, slots=True)
class DiffModel:
    left_label: str
    right_label: str
    hunks: tuple[Hunk, ...]
    stats: DiffStats
    truncated: bool
    identical: bool
    whitespace_only: bool
    line_endings_only: bool


type _Opcode = tuple[str, int, int, int, int]


def _squash(text: str) -> str:
    return "".join(text.split())


class _Budget:
    __slots__ = ("coarse", "remaining")

    def __init__(self, cells: int) -> None:
        self.remaining = cells
        self.coarse = False


def _anchors(a: list[str], b: list[str], a0: int, a1: int, b0: int, b1: int) -> list[tuple[int, int]]:
    """Longest increasing run of lines that occur exactly once on both sides (patience diff)."""
    count_a: dict[str, int] = {}
    for i in range(a0, a1):
        count_a[a[i]] = count_a.get(a[i], 0) + 1
    count_b: dict[str, int] = {}
    where_b: dict[str, int] = {}
    for j in range(b0, b1):
        count_b[b[j]] = count_b.get(b[j], 0) + 1
        where_b[b[j]] = j
    pairs = [(i, where_b[a[i]]) for i in range(a0, a1) if count_a[a[i]] == 1 and count_b.get(a[i]) == 1]
    tails: list[int] = []
    tail_index: list[int] = []
    previous = [-1] * len(pairs)
    for k, (_, j) in enumerate(pairs):
        pos = bisect.bisect_left(tails, j)
        if pos == len(tails):
            tails.append(j)
            tail_index.append(k)
        else:
            tails[pos] = j
            tail_index[pos] = k
        previous[k] = tail_index[pos - 1] if pos else -1
    chain: list[tuple[int, int]] = []
    k = tail_index[-1] if tail_index else -1
    while k >= 0:
        chain.append(pairs[k])
        k = previous[k]
    return chain[::-1]


def _diff_region(
    a: list[str],
    b: list[str],
    a0: int,
    a1: int,
    b0: int,
    b1: int,
    out: list[_Opcode],
    budget: _Budget,
    depth: int,
) -> None:
    while a0 < a1 and b0 < b1 and a[a0] == b[b0]:
        out.append(("equal", a0, a0 + 1, b0, b0 + 1))
        a0 += 1
        b0 += 1
    tail: list[_Opcode] = []
    while a0 < a1 and b0 < b1 and a[a1 - 1] == b[b1 - 1]:
        tail.append(("equal", a1 - 1, a1, b1 - 1, b1))
        a1 -= 1
        b1 -= 1
    if a0 == a1 and b0 == b1:
        pass
    elif a0 == a1:
        out.append(("insert", a0, a0, b0, b1))
    elif b0 == b1:
        out.append(("delete", a0, a1, b0, b0))
    elif (a1 - a0) * (b1 - b0) <= _SMALL_REGION_CELLS and budget.remaining >= (a1 - a0) * (b1 - b0):
        budget.remaining -= (a1 - a0) * (b1 - b0)
        matcher = difflib.SequenceMatcher(None, a[a0:a1], b[b0:b1], autojunk=False)
        out.extend((tag, a0 + i1, a0 + i2, b0 + j1, b0 + j2) for tag, i1, i2, j1, j2 in matcher.get_opcodes())
    elif depth < _MAX_DEPTH and budget.remaining >= (a1 - a0) + (b1 - b0):
        budget.remaining -= (a1 - a0) + (b1 - b0)
        anchors = _anchors(a, b, a0, a1, b0, b1)
        if not anchors:
            budget.coarse = True
            out.append(("replace", a0, a1, b0, b1))
        else:
            pa, pb = a0, b0
            for i, j in anchors:
                _diff_region(a, b, pa, i, pb, j, out, budget, depth + 1)
                out.append(("equal", i, i + 1, j, j + 1))
                pa, pb = i + 1, j + 1
            _diff_region(a, b, pa, a1, pb, b1, out, budget, depth + 1)
    else:
        budget.coarse = True
        out.append(("replace", a0, a1, b0, b1))
    out.extend(reversed(tail))


def _merge_runs(codes: list[_Opcode]) -> list[_Opcode]:
    merged: list[_Opcode] = []
    for tag, i1, i2, j1, j2 in codes:
        if i1 == i2 and j1 == j2:
            continue
        if merged:
            ptag, pi1, pi2, pj1, pj2 = merged[-1]
            if pi2 == i1 and pj2 == j1 and (ptag == tag or (ptag != "equal" and tag != "equal")):
                kind = tag if ptag == tag else "replace"
                merged[-1] = (kind, pi1, i2, pj1, j2)
                continue
        merged.append((tag, i1, i2, j1, j2))
    return [
        (("delete" if j1 == j2 else "insert" if i1 == i2 else tag), i1, i2, j1, j2)
        if tag == "replace"
        else (tag, i1, i2, j1, j2)
        for tag, i1, i2, j1, j2 in merged
    ]


def sequence_opcodes(a: list[str], b: list[str], limits: DiffLimits) -> tuple[list[_Opcode], bool]:
    """Edit script from ``a`` to ``b`` (difflib opcodes) and whether any region became a coarse replace."""
    if len(a) + len(b) > limits.exact_max_lines:
        limit = min(len(a), len(b))
        prefix = 0
        while prefix < limit and a[prefix] == b[prefix]:
            prefix += 1
        suffix = 0
        while suffix < limit - prefix and a[-1 - suffix] == b[-1 - suffix]:
            suffix += 1
        codes: list[_Opcode] = [
            ("equal", 0, prefix, 0, prefix),
            ("replace", prefix, len(a) - suffix, prefix, len(b) - suffix),
        ]
        codes.append(("equal", len(a) - suffix, len(a), len(b) - suffix, len(b)))
        return _merge_runs(codes), True
    budget = _Budget(limits.work_budget)
    out: list[_Opcode] = []
    _diff_region(a, b, 0, len(a), 0, len(b), out, budget, 0)
    return _merge_runs(out), budget.coarse


def _groups(codes: list[_Opcode], context: int) -> Iterator[list[_Opcode]]:
    """Hunks with ``context`` equal lines around each change (the grouping rule difflib uses)."""
    codes = list(codes)
    if codes[0][0] == "equal":
        tag, i1, i2, j1, j2 = codes[0]
        codes[0] = (tag, max(i1, i2 - context), i2, max(j1, j2 - context), j2)
    if codes[-1][0] == "equal":
        tag, i1, i2, j1, j2 = codes[-1]
        codes[-1] = (tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context))
    group: list[_Opcode] = []
    for tag, i1, i2, j1, j2 in codes:
        if tag == "equal" and i2 - i1 > 2 * context:
            group.append((tag, i1, min(i2, i1 + context), j1, min(j2, j1 + context)))
            yield group
            group = []
            i1, j1 = max(i1, i2 - context), max(j1, j2 - context)
        group.append((tag, i1, i2, j1, j2))
    if group and not (len(group) == 1 and group[0][0] == "equal"):
        yield group


def diff_lines(
    left: str,
    right: str,
    limits: DiffLimits | None = None,
    labels: tuple[str, str] = ("left", "right"),
    line_endings_only: bool = False,
) -> DiffModel:
    limits = limits or DiffLimits()
    if left == right:
        return DiffModel(labels[0], labels[1], (), DiffStats(0, 0, 0), False, True, False, line_endings_only)
    a = left.split("\n")
    b = right.split("\n")
    codes, coarse = sequence_opcodes(a, b, limits)
    added = sum(j2 - j1 for tag, _, _, j1, j2 in codes if tag in ("insert", "replace"))
    removed = sum(i2 - i1 for tag, i1, i2, _, _ in codes if tag in ("delete", "replace"))
    hunks: list[Hunk] = []
    truncated = False
    for group in _groups(codes, limits.context):
        if len(hunks) >= limits.max_hunks:
            truncated = True
            break
        lines: list[DiffLine] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                lines.extend(DiffLine(Op.EQUAL, a[i], i + 1, j1 + (i - i1) + 1) for i in range(i1, i2))
                continue
            if tag in ("delete", "replace"):
                shown = min(i2 - i1, limits.max_block_lines)
                truncated |= shown < i2 - i1
                lines.extend(DiffLine(Op.DELETE, a[i], i + 1, None) for i in range(i1, i1 + shown))
            if tag in ("insert", "replace"):
                shown = min(j2 - j1, limits.max_block_lines)
                truncated |= shown < j2 - j1
                lines.extend(DiffLine(Op.INSERT, b[j], None, j + 1) for j in range(j1, j1 + shown))
        first, last = group[0], group[-1]
        hunks.append(
            Hunk(first[1] + 1, last[2] - first[1], first[3] + 1, last[4] - first[3], tuple(lines), coarse)
        )
    whitespace_only = _squash(left) == _squash(right)
    return DiffModel(
        labels[0],
        labels[1],
        tuple(hunks),
        DiffStats(added, removed, len(hunks)),
        truncated or coarse,
        False,
        whitespace_only,
        line_endings_only,
    )


def diff_raw(
    left: bytes, right: bytes, limits: DiffLimits | None = None, labels: tuple[str, str] = ("left", "right")
) -> DiffModel:
    """Diff two file contents; flags differences that are only line endings or an encoding mark."""
    a = left.decode("utf-8", errors="replace").removeprefix("\N{ZERO WIDTH NO-BREAK SPACE}")
    b = right.decode("utf-8", errors="replace").removeprefix("\N{ZERO WIDTH NO-BREAK SPACE}")
    a_norm, b_norm = _BREAK.sub("\n", a), _BREAK.sub("\n", b)
    return diff_lines(a_norm, b_norm, limits, labels, line_endings_only=left != right and a_norm == b_norm)


def diff_words(left: str, right: str, limits: DiffLimits | None = None) -> list[tuple[Op, str]]:
    """Intra-line segments: equal, deleted and inserted runs of words, spaces and punctuation."""
    limits = limits or DiffLimits()
    if left == right:
        return [(Op.EQUAL, left)] if left else []
    if len(left) + len(right) > limits.word_max_chars:
        return [(op, text) for op, text in ((Op.DELETE, left), (Op.INSERT, right)) if text]
    a = _TOKEN.findall(left)
    b = _TOKEN.findall(right)
    out: list[tuple[Op, str]] = []

    def push(op: Op, text: str) -> None:
        if not text:
            return
        if out and out[-1][0] is op:
            out[-1] = (op, out[-1][1] + text)
        else:
            out.append((op, text))

    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag == "equal":
            push(Op.EQUAL, "".join(a[i1:i2]))
            continue
        push(Op.DELETE, "".join(a[i1:i2]))
        push(Op.INSERT, "".join(b[j1:j2]))
    return out


def to_unified(model: DiffModel) -> str:
    """Unified-diff text for copying to the clipboard. Callers must never log it."""
    if model.identical:
        return ""
    out = [f"--- {model.left_label}", f"+++ {model.right_label}"]
    for hunk in model.hunks:
        out.append(f"@@ -{hunk.left_start},{hunk.left_count} +{hunk.right_start},{hunk.right_count} @@")
        sign = {Op.EQUAL: " ", Op.DELETE: "-", Op.INSERT: "+"}
        out.extend(sign[line.op] + line.text for line in hunk.lines)
    if model.truncated:
        out.append("@@ output truncated @@")
    return "\n".join(out) + "\n"
