"""Line-by-line Markdown classification for syntax highlighting.

It only decides how characters look; it never changes text, so an imperfect guess costs styling,
never data. Every scan is linear in the line length and bounded (SEC8): long lines get block-level
styling only, and bracket and emphasis stacks have fixed depth limits.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import IntFlag

from paperless_notes.core.security.limits import MAX_BRACKET_DEPTH, MAX_INLINE_CHARS, MAX_QUOTE_DEPTH


class Style(IntFlag):
    NONE = 0
    MARKER = 1 << 0
    HEADING = 1 << 1
    STRONG = 1 << 2
    EMPHASIS = 1 << 3
    STRIKE = 1 << 4
    CODE = 1 << 5
    CODE_BLOCK = 1 << 6
    LINK = 1 << 7
    URL = 1 << 8
    QUOTE = 1 << 9
    HTML = 1 << 10
    UNDERLINE = 1 << 11
    LIST = 1 << 12
    TASK_DONE = 1 << 13
    RULE = 1 << 14
    META = 1 << 15
    ESCAPE = 1 << 16
    HARD_BREAK = 1 << 17
    TABLE = 1 << 18
    FOOTNOTE = 1 << 19


class State:
    """Block state carried from one line to the next (QSyntaxHighlighter block state)."""

    NORMAL = 0
    FRONT_MATTER = 1
    HTML_COMMENT = 2
    TABLE = 3
    FENCE_BASE = 16

    @staticmethod
    def fence(char: str, length: int, indent: int) -> int:
        return State.FENCE_BASE + (min(length, 255) << 3) + (min(indent, 3) << 1) + (char == "~")

    @staticmethod
    def fence_info(state: int) -> tuple[str, int, int] | None:
        if state < State.FENCE_BASE:
            return None
        v = state - State.FENCE_BASE
        return ("~" if v & 1 else "`", v >> 3, (v >> 1) & 3)


@dataclass(frozen=True, slots=True)
class Span:
    start: int
    end: int
    style: Style


@dataclass(frozen=True, slots=True)
class LineResult:
    spans: list[Span]
    state: int
    heading: int = 0


_ATX = re.compile(r" {0,3}(#{1,6})(?=[ \t]|$)")
_ATX_CLOSE = re.compile(r"[ \t]+#+[ \t]*$")
_FENCE = re.compile(r"( {0,3})(`{3,}|~{3,})(.*)$")
_RULE = re.compile(r" {0,3}([-*_])(?:[ \t]*\1){2,}[ \t]*$")
_SETEXT = re.compile(r" {0,3}(=+|-+)[ \t]*$")
_LIST = re.compile(r"( *)([-+*]|\d{1,9}[.)])([ \t]+|$)")
_TASK = re.compile(r"\[([ xX])\](?=[ \t]|$)")
_TABLE_DELIM = re.compile(r" {0,3}\|?[ \t]*:?-+:?[ \t]*(\|[ \t]*:?-+:?[ \t]*)*\|?[ \t]*$")
_REF_DEF = re.compile(r" {0,3}\[(\^?)[^\]]{1,999}\]:")
_HTML_BLOCK = re.compile(r" {0,3}<(?:!--|/?[A-Za-z][A-Za-z0-9-]*(?:[\s/>]|$)|\?|![A-Z])")
_AUTOLINK = re.compile(
    r"<(?:[A-Za-z][A-Za-z0-9+.-]{1,31}:[^<>\s]*|[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+)>"
)
_INLINE_HTML = re.compile(r"</?[A-Za-z][A-Za-z0-9-]{0,40}(?:\s[^<>]{0,1000})?/?>|<!--.{0,2000}?-->")
_BARE_START = re.compile(r"https?://|www\.")
_URL_TRAILING = frozenset(".,;:!?\"'*_~")
_ESCAPABLE = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")
_PUNCT = frozenset("!\"#$%&'()*+,-./:;<=>?@[\\]^_`{|}~")


_SPECIAL = re.compile(r"[#*_~`\[\]<>|\\!+=:-]|^[ \t\d]|www\.")
_PLAIN = LineResult([], State.NORMAL)


def _is_plain(line: str, next_line: str | None) -> bool:
    """Most prose lines contain no Markdown syntax at all; they need no styling."""
    if _SPECIAL.search(line) is not None:
        return False
    return next_line is None or not (
        next_line.lstrip(" ").startswith(("=", "-")) and _SETEXT.fullmatch(next_line)
    )


def is_blank(line: str) -> bool:
    return not line.strip()


def is_setext_underline(line: str) -> bool:
    return _SETEXT.fullmatch(line) is not None


def is_table_delimiter(line: str) -> bool:
    return "-" in line and _TABLE_DELIM.fullmatch(line) is not None


def lex_line(
    line: str, prev_state: int, first_line: bool = False, next_line: str | None = None
) -> LineResult:
    fence = State.fence_info(prev_state)
    if fence is not None:
        return _in_fence(line, fence)
    if prev_state == State.FRONT_MATTER:
        if line.rstrip() in ("---", "..."):
            return LineResult([Span(0, len(line), Style.META | Style.MARKER)], State.NORMAL)
        return LineResult([Span(0, len(line), Style.META)], State.FRONT_MATTER)
    if prev_state == State.HTML_COMMENT:
        end = line.find("-->")
        if end < 0:
            return LineResult([Span(0, len(line), Style.HTML)], State.HTML_COMMENT)
        rest = _inline(line[end + 3 :], end + 3)
        return LineResult([Span(0, end + 3, Style.HTML), *rest], State.NORMAL)
    if _is_plain(line, next_line):
        return _PLAIN
    if first_line and line.rstrip() == "---":
        return LineResult([Span(0, len(line), Style.META | Style.MARKER)], State.FRONT_MATTER)
    m = _FENCE.match(line)
    if m and not (m.group(2)[0] == "`" and "`" in m.group(3)):
        indent, marker = len(m.group(1)), m.group(2)
        spans = [Span(0, len(line), Style.CODE_BLOCK), Span(indent, indent + len(marker), Style.MARKER)]
        return LineResult(spans, State.fence(marker[0], len(marker), indent))
    if _RULE.fullmatch(line):
        return LineResult([Span(0, len(line), Style.RULE | Style.MARKER)], State.NORMAL)
    m = _ATX.match(line)
    if m:
        return _heading(line, m)
    if next_line is not None and not is_blank(line) and _SETEXT.fullmatch(next_line):
        level = 1 if next_line.strip()[0] == "=" else 2
        spans = [Span(0, len(line), Style.HEADING), *_inline(line, 0)]
        return LineResult(spans, State.NORMAL, heading=level)
    if prev_state == State.TABLE and "|" in line and not is_blank(line):
        return LineResult(_table_row(line), State.TABLE)
    if is_table_delimiter(line) and "|" in line:
        return LineResult([Span(0, len(line), Style.TABLE | Style.MARKER)], State.TABLE)
    if next_line is not None and "|" in line and is_table_delimiter(next_line) and "|" in next_line:
        return LineResult(_table_row(line), State.NORMAL)
    stripped = line.lstrip(" ")
    if stripped.startswith("<!--") and "-->" not in stripped:
        return LineResult([Span(0, len(line), Style.HTML)], State.HTML_COMMENT)
    if _HTML_BLOCK.match(line) and not _AUTOLINK.match(stripped):
        return LineResult([Span(0, len(line), Style.HTML)], State.NORMAL)
    m = _REF_DEF.match(line)
    if m:
        style = Style.FOOTNOTE if m.group(1) else Style.LINK
        return LineResult(
            [Span(0, m.end(), style | Style.MARKER), *_inline(line[m.end() :], m.end())], State.NORMAL
        )
    prefix_spans: list[Span] = []
    offset = _quote_prefix(line, prefix_spans)
    offset = _list_prefix(line, offset, prefix_spans)
    prefix_spans.extend(_inline(line[offset:], offset))
    return LineResult(prefix_spans, State.NORMAL)


def _in_fence(line: str, fence: tuple[str, int, int]) -> LineResult:
    char, length, _indent = fence
    stripped = line.lstrip(" ")
    lead = len(line) - len(stripped)
    run = len(stripped) - len(stripped.lstrip(char))
    if lead <= 3 and run >= length and not stripped[run:].strip():
        return LineResult(
            [Span(0, len(line), Style.CODE_BLOCK), Span(lead, lead + run, Style.MARKER)], State.NORMAL
        )
    return LineResult([Span(0, len(line), Style.CODE_BLOCK)], State.fence(char, length, _indent))


def _heading(line: str, m: re.Match[str]) -> LineResult:
    level = len(m.group(1))
    spans = [Span(0, len(line), Style.HEADING), Span(0, m.end(1), Style.MARKER)]
    content_end = len(line)
    close = _ATX_CLOSE.search(line, m.end(1))
    if close:
        spans.append(Span(close.start(), len(line), Style.MARKER))
        content_end = close.start()
    spans.extend(_inline(line[m.end(1) : content_end], m.end(1)))
    return LineResult(spans, State.NORMAL, heading=level)


def _table_row(line: str) -> list[Span]:
    spans = [Span(0, len(line), Style.TABLE)]
    escaped = False
    for i, ch in enumerate(line):
        if ch == "|" and not escaped:
            spans.append(Span(i, i + 1, Style.MARKER))
        escaped = ch == "\\" and not escaped
    return spans


def _quote_prefix(line: str, spans: list[Span]) -> int:
    pos = 0
    depth = 0
    while depth < MAX_QUOTE_DEPTH:
        lead = 0
        while lead < 3 and pos + lead < len(line) and line[pos + lead] == " ":
            lead += 1
        if pos + lead < len(line) and line[pos + lead] == ">":
            end = pos + lead + 1
            if end < len(line) and line[end] == " ":
                end += 1
            spans.append(Span(pos + lead, pos + lead + 1, Style.MARKER))
            pos = end
            depth += 1
        else:
            break
    if depth:
        spans.insert(0, Span(0, len(line), Style.QUOTE))
    return pos


def _list_prefix(line: str, offset: int, spans: list[Span]) -> int:
    m = _LIST.match(line, offset)
    if not m:
        return offset
    marker_start = m.start(2)
    spans.append(Span(marker_start, m.end(2), Style.LIST | Style.MARKER))
    pos = m.end()
    task = _TASK.match(line, pos)
    if task:
        spans.append(Span(task.start(), task.end(), Style.LIST | Style.MARKER))
        if task.group(1) in "xX":
            spans.append(Span(task.end(), len(line), Style.TASK_DONE))
        pos = task.end()
    return pos


@dataclass(slots=True)
class _Delim:
    char: str
    pos: int
    length: int
    can_open: bool
    can_close: bool
    orig: int


def inline_spans(text: str, base: int = 0) -> list[Span]:
    """Inline spans (code, links, URLs, raw HTML, emphasis) of ``text`` as if it were paragraph text."""
    return _inline(text, base)


def _inline(text: str, base: int) -> list[Span]:
    if not text:
        return []
    if len(text) > MAX_INLINE_CHARS:
        return []
    spans: list[Span] = []
    trailing = len(text) - len(text.rstrip(" "))
    if trailing >= 2 and text.strip():
        spans.append(Span(base + len(text) - trailing, base + len(text), Style.HARD_BREAK))
    elif text.endswith("\\"):
        spans.append(Span(base + len(text) - 1, base + len(text), Style.HARD_BREAK | Style.MARKER))
    protected = _code_spans(text, base, spans)
    _raw_spans(text, base, spans, protected)
    _links(text, base, spans, protected)
    _emphasis(text, base, spans, protected)
    return spans


def _code_spans(text: str, base: int, spans: list[Span]) -> bytearray:
    """Mark code spans, escapes and raw HTML regions; returns a mask of characters that are literal."""
    protected = bytearray(len(text))
    runs: list[tuple[int, int]] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "\\" and i + 1 < n and text[i + 1] in _ESCAPABLE:
            spans.append(Span(base + i, base + i + 1, Style.ESCAPE | Style.MARKER))
            protected[i] = protected[i + 1] = 1
            i += 2
            continue
        if ch == "`":
            j = i
            while j < n and text[j] == "`":
                j += 1
            runs.append((i, j - i))
            i = j
            continue
        i += 1
    by_length: dict[int, list[int]] = {}
    for index, (_start, length) in enumerate(runs):
        by_length.setdefault(length, []).append(index)
    used = [False] * len(runs)
    cursors = dict.fromkeys(by_length, 0)
    for index, (start, length) in enumerate(runs):
        if used[index]:
            continue
        candidates = by_length[length]
        k = cursors[length]
        while k < len(candidates) and candidates[k] <= index:
            k += 1
        cursors[length] = k
        if k >= len(candidates):
            continue
        close_index = candidates[k]
        cursors[length] = k + 1
        used[index] = used[close_index] = True
        close_start = runs[close_index][0]
        spans.append(Span(base + start, base + close_start + length, Style.CODE))
        spans.append(Span(base + start, base + start + length, Style.MARKER))
        spans.append(Span(base + close_start, base + close_start + length, Style.MARKER))
        for p in range(start, close_start + length):
            protected[p] = 1
    return protected


def _raw_spans(text: str, base: int, spans: list[Span], protected: bytearray) -> None:
    for m in _AUTOLINK.finditer(text):
        if not protected[m.start()]:
            spans.append(Span(base + m.start(), base + m.end(), Style.URL))
            _protect(protected, m.start(), m.end())
    underline_open: int | None = None
    for m in _INLINE_HTML.finditer(text):
        if protected[m.start()]:
            continue
        tag = m.group().lower()
        spans.append(Span(base + m.start(), base + m.end(), Style.HTML | Style.MARKER))
        if tag == "<u>":
            underline_open = m.end()
        elif tag == "</u>" and underline_open is not None:
            spans.append(Span(base + underline_open, base + m.start(), Style.UNDERLINE))
            underline_open = None
        _protect(protected, m.start(), m.end())
    for m in _BARE_START.finditer(text):
        start = m.start()
        if protected[start] or (start > 0 and (text[start - 1].isalnum() or text[start - 1] in "/@.")):
            continue
        end = bare_url_end(text, start)
        if end > m.end():
            spans.append(Span(base + start, base + end, Style.URL))
            _protect(protected, start, end)


def bare_url_end(text: str, start: int) -> int:
    """End of a bare URL: up to whitespace or angle brackets, then trailing punctuation and closing
    parentheses without a matching opener are left to the prose around it. Linear in the URL length."""
    end = start
    while end < len(text) and not text[end].isspace() and text[end] not in "<>":
        end += 1
    opens = text.count("(", start, end)
    closes = text.count(")", start, end)
    while end > start:
        last = text[end - 1]
        if last in _URL_TRAILING:
            end -= 1
        elif last == ")" and opens < closes:
            closes -= 1
            end -= 1
        else:
            break
    return end


def _protect(mask: bytearray, start: int, end: int) -> None:
    mask[start:end] = b"\x01" * (end - start)


def _links(text: str, base: int, spans: list[Span], protected: bytearray) -> None:
    stack: list[int] = []
    n = len(text)
    i = 0
    while i < n:
        ch = text[i]
        if protected[i]:
            i += 1
            continue
        if ch == "[":
            if len(stack) < MAX_BRACKET_DEPTH:
                stack.append(i)
        elif ch == "]" and stack:
            open_pos = stack.pop()
            image = open_pos > 0 and text[open_pos - 1] == "!" and not protected[open_pos - 1]
            start = open_pos - 1 if image else open_pos
            end = _link_destination(text, i + 1, protected)
            footnote = text.startswith("^", open_pos + 1)
            if end is not None or footnote or (i + 1 < n and text[i + 1] == "["):
                if end is None and i + 1 < n and text[i + 1] == "[":
                    close = text.find("]", i + 2)
                    end = close + 1 if close > 0 else None
                style = Style.FOOTNOTE if footnote else Style.LINK
                spans.append(Span(base + open_pos + 1, base + i, style))
                spans.append(Span(base + start, base + open_pos + 1, Style.MARKER))
                spans.append(Span(base + i, base + (end if end is not None else i + 1), Style.MARKER))
                if end is not None:
                    spans.append(Span(base + i + 1, base + end, Style.URL | Style.MARKER))
                    _protect(protected, i + 1, end)
                    i = end
                    stack.clear()
                    continue
        i += 1


def _link_destination(text: str, pos: int, protected: bytearray) -> int | None:
    if pos >= len(text) or text[pos] != "(":
        return None
    depth = 0
    i = pos
    if text.find(")", pos) < 0:
        return None
    limit = min(len(text), pos + 512)
    while i < limit:
        ch = text[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "(":
            depth += 1
            if depth > MAX_BRACKET_DEPTH:
                return None
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return None


def _flanking(text: str, start: int, end: int) -> tuple[bool, bool]:
    before = text[start - 1] if start > 0 else " "
    after = text[end] if end < len(text) else " "
    left = not after.isspace() and (after not in _PUNCT or before.isspace() or before in _PUNCT)
    right = not before.isspace() and (before not in _PUNCT or after.isspace() or after in _PUNCT)
    return left, right


def _delimiters(text: str, protected: bytearray) -> list[_Delim]:
    delims: list[_Delim] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch not in "*_~" or protected[i]:
            i += 1
            continue
        j = i
        while j < n and text[j] == ch and not protected[j]:
            j += 1
        left, right = _flanking(text, i, j)
        if ch == "_":
            before = text[i - 1] if i > 0 else " "
            after = text[j] if j < n else " "
            can_open = left and (not right or before in _PUNCT)
            can_close = right and (not left or after in _PUNCT)
        else:
            can_open, can_close = left, right
        if ch != "~" or j - i == 2:
            delims.append(_Delim(ch, i, j - i, can_open, can_close, j - i))
        i = j
    return delims


def _emphasis(text: str, base: int, spans: list[Span], protected: bytearray) -> None:
    """CommonMark delimiter matching with a stack of live openers; amortised linear."""
    stack: list[_Delim] = []
    bottom: dict[tuple[str, bool, int], int] = {}
    for d in _delimiters(text, protected):
        while d.can_close and d.length > 0:
            key = (d.char, d.can_open, d.orig % 3)
            floor = bottom.get(key, 0)
            found = -1
            for k in range(len(stack) - 1, floor - 1, -1):
                o = stack[k]
                if o.char != d.char:
                    continue
                if (
                    (o.can_close or d.can_open)
                    and (o.orig + d.orig) % 3 == 0
                    and not (o.orig % 3 == 0 and d.orig % 3 == 0)
                ):
                    continue
                found = k
                break
            if found < 0:
                bottom[key] = len(stack)
                break
            o = stack[found]
            if d.char == "~":
                use, style = 2, Style.STRIKE
            else:
                use = 2 if o.length >= 2 and d.length >= 2 else 1
                style = Style.STRONG if use == 2 else Style.EMPHASIS
            o_start = o.pos + o.length - use
            spans.append(Span(base + o_start, base + d.pos + use, style))
            spans.append(Span(base + o_start, base + o_start + use, Style.MARKER))
            spans.append(Span(base + d.pos, base + d.pos + use, Style.MARKER))
            o.length -= use
            d.length -= use
            d.pos += use
            del stack[found + 1 :]
            if o.length == 0:
                stack.pop()
            for k2, v in bottom.items():
                if v > len(stack):
                    bottom[k2] = len(stack)
        if d.can_open and d.length > 0 and len(stack) < MAX_INLINE_CHARS:
            stack.append(d)


def flatten(spans: list[Span], length: int) -> list[tuple[int, int, int]]:
    """Non-overlapping runs ``(start, end, style bits)`` with the union of styles, clipped to ``length``."""
    events: list[tuple[int, int, int]] = []
    for s in spans:
        a = s.start if s.start > 0 else 0
        b = s.end if s.end < length else length
        if a < b:
            bits = int(s.style)
            events.append((a, 1, bits))
            events.append((b, -1, bits))
    if not events:
        return []
    events.sort(key=_first)
    active: dict[int, int] = {}
    runs: list[tuple[int, int, int]] = []
    pos = events[0][0]
    i = 0
    n = len(events)
    while i < n:
        point = events[i][0]
        if point > pos:
            if active:
                style = 0
                for bits in active:
                    style |= bits
                if runs and runs[-1][2] == style and runs[-1][1] == pos:
                    runs[-1] = (runs[-1][0], point, style)
                else:
                    runs.append((pos, point, style))
            pos = point
        while i < n and events[i][0] == point:
            _, delta, bits = events[i]
            count = active.get(bits, 0) + delta
            if count:
                active[bits] = count
            else:
                active.pop(bits)
            i += 1
    return runs


def _first(event: tuple[int, int, int]) -> int:
    return event[0]
