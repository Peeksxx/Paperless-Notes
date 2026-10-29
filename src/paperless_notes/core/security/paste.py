"""Paste and drop policy.

Paste is plain text unless the clipboard's HTML carries real structure (headings, emphasis, links,
lists, quotes, code, tables); then it is converted to Markdown through an allow-list. Scripts, styles,
forms, embedded objects and images are dropped (an image's alt text stays as text), links survive only
when the link policy allows them, and ordinary text is escaped so it cannot turn into Markdown or HTML
by accident. Conversion is bounded: too deep or too large HTML returns None and the caller pastes the
plain text instead. Dropped files are only ever opened as notes, never embedded.
"""

from __future__ import annotations

import ntpath
import re
import string
import unicodedata
from dataclasses import dataclass, field
from html.parser import HTMLParser

from paperless_notes.core.security.links import link_allowed

MAX_PASTE_CHARS = 5_000_000
MAX_HTML_CHARS = 2_000_000
MAX_HTML_DEPTH = 64
MAX_HTML_ELEMENTS = 50_000
NOTE_EXTENSIONS = frozenset({".md", ".markdown", ".txt"})
_STRIP = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f" + chr(0xFDD0) + "-" + chr(0xFDEF) + chr(0xFFFE) + chr(0xFFFF) + "]"
)
_ESCAPE = re.compile(r"([\\`*_\[\]<>])")
_LINE_START = re.compile(r"^([ \t]*)([#>+-]|\d+[.)])(?=[ \t]|$)", re.MULTILINE)
_STRUCTURE = re.compile(
    r"<\s*(b|strong|i|em|s|strike|del|u|a|h[1-6]|ul|ol|li|blockquote|code|pre|table|br)\b", re.IGNORECASE
)
_BACKTICKS = re.compile(r"`+")


def normalize_pasted_text(text: str) -> str:
    """Unify line breaks and drop control and noncharacter code points the editor cannot keep."""
    text = text[:MAX_PASTE_CHARS]
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\N{PARAGRAPH SEPARATOR}", "\n")
    return _STRIP.sub("", text)


def paste_text(plain: str | None, html: str | None = None, rich: bool = False) -> str:
    if rich and html:
        return normalize_pasted_text(html_to_markdown(html))
    return normalize_pasted_text(plain or "")


def clipboard_fragment(html: str) -> str:
    """The copied part of Windows clipboard HTML (CF_HTML header and StartFragment markers removed)."""
    start = html.find("<!--StartFragment-->")
    end = html.find("<!--EndFragment-->")
    if start >= 0 and end > start:
        return html[start + len("<!--StartFragment-->") : end]
    if html.startswith("Version:"):
        tag = html.find("<")
        return html[tag:] if tag >= 0 else ""
    return html


def has_structure(html: str) -> bool:
    """True when HTML carries formatting worth converting; styled plain text pastes as plain text."""
    return _STRUCTURE.search(html[:MAX_HTML_CHARS]) is not None


def _escape_line_start(match: re.Match[str]) -> str:
    lead, marker = match.group(1), match.group(2)
    if marker[0].isdigit():
        return lead + marker[:-1] + "\\" + marker[-1]
    return lead + "\\" + marker


def escape_markdown(text: str) -> str:
    """Make ordinary text stay ordinary text in Markdown."""
    return _LINE_START.sub(_escape_line_start, _ESCAPE.sub(r"\\\1", text))


_INLINE = {
    "b": "b",
    "strong": "b",
    "i": "i",
    "em": "i",
    "s": "s",
    "strike": "s",
    "del": "s",
    "u": "u",
}
_MARKS = {"b": ("**", "**"), "i": ("*", "*"), "s": ("~~", "~~"), "u": ("<u>", "</u>")}
_PREFIX_ONLY = re.compile(r"[ \t]*(?:(?:[-+*>]|\d+[.)]|#{1,6})[ \t]+)*\Z")


def _punctuation(ch: str) -> bool:
    return ch in string.punctuation or unicodedata.category(ch)[0] in "PS"


def _plain_char(ch: str) -> bool:
    """A character that is neither whitespace nor punctuation, so a marker touching it must flank it."""
    return bool(ch) and not ch.isspace() and not _punctuation(ch)


def _tail_length(text: str) -> int:
    end = len(text)
    while end > 0 and (text[end - 1].isspace() or _punctuation(text[end - 1])):
        end -= 1
    return len(text) - end


Run = tuple[tuple[str, ...], str, bool]


@dataclass
class _InlineWriter:
    """Turns runs of styled text into Markdown that every CommonMark reader shows as intended: spaces
    and, where needed, punctuation stay outside the markers, runs with the same style merge, empty styled
    runs vanish, and styles close at a line break and reopen on the next line."""

    previous: str
    line_start: bool
    out: list[str] = field(default_factory=list)
    open: list[str] = field(default_factory=list)
    pending: str = ""
    inside: int = 0
    floor: int = 0

    def put(self, text: str, raw: bool = False) -> None:
        if not raw and self.line_start:
            text = text.lstrip(" ")
        if not text:
            return
        if not raw:
            text = _ESCAPE.sub(r"\\\1", text)
            if self.line_start:
                text = _LINE_START.sub(_escape_line_start, text, count=1)
        self.out.append(text)
        self.previous = text[-1]
        self.line_start = False
        self.inside += len(text)

    def marker(self, text: str) -> None:
        self.out.append(text)
        self.previous = text[-1]
        self.line_start = False

    def close(self, keep: int, following: str) -> None:
        """Close the styles above ``keep``; the held-back end of the last text goes inside or outside."""
        tail, self.pending = self.pending, ""
        keep = max(keep, self.floor)
        if keep >= len(self.open):
            self.put(tail)
            return
        split = next((i for i, ch in enumerate(tail) if ch.isspace()), len(tail))
        punctuation, space = tail[:split], tail[split:]
        outside = not space and _plain_char(following) and self.inside > 0 and bool(punctuation)
        if not outside:
            self.put(punctuation)
        while len(self.open) > keep:
            self.marker(_MARKS[self.open.pop()][1])
        if outside:
            self.put(punctuation)
        self.put(space)

    def write(self, runs: list[Run]) -> str:
        pieces: list[Run] = []
        for styles, text, raw in runs:
            if raw:
                pieces.append((styles, text, True))
                continue
            for index, part in enumerate(text.split("\n")):
                if index:
                    pieces.append(((), "\n", False))
                if part:
                    pieces.append((styles, part, False))
        for styles, text, raw in pieces:
            if text == "\n" and not raw:
                self.pending = self.pending.rstrip()
                self.close(0, "\n")
                self.out.append("\n")
                self.previous, self.line_start = "\n", True
                continue
            if not raw and not text.strip():
                self.pending += text
                continue
            target: list[str] = []
            for style in styles:
                if style in _MARKS and style not in target:
                    target.append(style)
            keep = 0
            while keep < len(self.open) and keep < len(target) and self.open[keep] == target[keep]:
                keep += 1
            self.close(keep, text[0])
            body = text
            opening = target[keep:]
            if opening:
                if not raw:
                    lead = len(body) - len(body.lstrip())
                    self.put(body[:lead])
                    body = body[lead:]
                    if _plain_char(self.previous):
                        count = 0
                        while count < len(body) and _punctuation(body[count]):
                            count += 1
                        if 0 < count < len(body):
                            self.put(body[:count])
                            body = body[count:]
                for style in opening:
                    self.marker(_MARKS[style][0])
                    self.open.append(style)
                self.inside = 0
            if raw:
                self.put(body, raw=True)
                continue
            cut = len(body) - _tail_length(body)
            self.put(body[:cut])
            self.pending = body[cut:]
        self.close(0, "")
        return "".join(self.out)


_BLOCK = {"p", "div", "section", "article", "header", "footer", "main", "figure"}
_DROP = {
    "script",
    "style",
    "head",
    "title",
    "noscript",
    "template",
    "svg",
    "math",
    "iframe",
    "object",
    "embed",
    "form",
    "input",
    "button",
    "select",
    "textarea",
    "audio",
    "video",
    "canvas",
    "frameset",
    "frame",
}
_VOID = {"br", "img", "hr", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}


class _TooComplexError(Exception):
    pass


class _Converter(HTMLParser):
    def __init__(self, inherited: tuple[str, ...] = ()) -> None:
        super().__init__(convert_charrefs=True)
        self.inherited = tuple(style for style in inherited if style in _MARKS)
        self.out: list[str] = []
        self.lists: list[list[int]] = []
        self.quotes: list[int] = []
        self.prefixes: list[str] = []
        self.want_blank = False
        self.links: list[str | None] = []
        self.skip = 0
        self.pre = 0
        self.pre_parts: list[str] | None = None
        self.code = 0
        self.depth = 0
        self.elements = 0
        self.table: list[list[str]] | None = None
        self.cell: list[str] | None = None
        self.styles: list[str] = []
        self.runs: list[Run] = []
        self.line = ""

    def _write(self, text: str) -> None:
        if self.pre and self.pre_parts is not None:
            self.pre_parts.append(text)
            return
        if self.cell is not None:
            self.cell.append(text)
            return
        for index, part in enumerate(text.split("\n")):
            if index:
                self.out.append("\n")
                self.line = ""
            if part:
                if not self.line:
                    if self.want_blank:
                        quote = "".join(p for p in self.prefixes if p.startswith(">")).rstrip()
                        self.out.append(quote + "\n")
                    if self.prefixes:
                        self.out.append("".join(self.prefixes))
                        self.line = "".join(self.prefixes)
                self.want_blank = False
                self.out.append(part)
                self.line += part

    def _close_containers(self, keep: int) -> None:
        self.flush()
        self._end_line()
        self.want_blank = False
        del self.prefixes[keep:]

    def _end_line(self) -> None:
        if self.line and _PREFIX_ONLY.match(self.line) is None:
            self._write("\n")

    def flush(self) -> None:
        """Write the styled text gathered so far."""
        if not self.runs:
            return
        runs, self.runs = self.runs, []
        if self.cell is not None:
            previous, start = ("".join(self.cell[-1:]) or " ")[-1], False
        else:
            previous = self.line[-1:] or "\n"
            start = _PREFIX_ONLY.match(self.line) is not None
        if self.inherited:
            runs = [(self.inherited + styles, text, raw) for styles, text, raw in runs]
        writer = _InlineWriter(previous, start, open=list(self.inherited), floor=len(self.inherited))
        self._write(writer.write(runs))

    def _emit(self, text: str) -> None:
        if self.skip:
            return
        self.flush()
        self._write(text)

    def _text(self, text: str, raw: bool = False) -> None:
        if self.skip or not text:
            return
        if self.pre and self.pre_parts is not None:
            self.pre_parts.append(text)
            return
        self.runs.append((tuple(self.styles), text, raw))

    def _newline(self, blank: bool = False) -> None:
        """Start a new line, and with ``blank`` a new block. Inside a quote the blank line keeps its ``>``;
        right after a list marker nothing is added, so the item's first block stays on the marker line."""
        if self.skip or self.cell is not None:
            return
        self.flush()
        if self.line and _PREFIX_ONLY.match(self.line) is not None:
            return
        self._end_line()
        if blank and self.out:
            self.want_blank = True

    def finish_pre(self) -> None:
        """Close a preformatted block, including malformed clipboard HTML with no end tag."""
        if self.pre_parts is None:
            return
        body = "".join(self.pre_parts)
        longest = max((len(match.group()) for match in _BACKTICKS.finditer(body)), default=0)
        fence = "`" * max(3, longest + 1)
        self.pre = 0
        self.pre_parts = None
        separator = "" if body.endswith("\n") else "\n"
        self._emit(f"{fence}\n{body}{separator}{fence}\n")

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.elements += 1
        if self.elements > MAX_HTML_ELEMENTS:
            raise _TooComplexError
        if tag not in _VOID:
            self.depth += 1
            if self.depth > MAX_HTML_DEPTH:
                raise _TooComplexError
        if tag in _DROP:
            if tag not in _VOID:
                self.skip += 1
            return
        if self.skip:
            return
        if tag in _INLINE:
            if not self.pre:
                self.styles.append(_INLINE[tag])
        elif tag == "code" and not self.pre:
            self.code += 1
            self._text("`", raw=True)
        elif tag == "br":
            self._text(" " if self.cell is not None else "\n")
        elif tag in _BLOCK:
            self._newline(blank=True)
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self._newline(blank=True)
            self._emit("#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self._newline()
            self.lists.append([1 if tag == "ol" else 0, len(self.prefixes)])
        elif tag == "li":
            counter = self.lists[-1] if self.lists else [0, len(self.prefixes)]
            self._close_containers(counter[1])
            marker = f"{counter[0]}. " if counter[0] else "- "
            if counter[0]:
                counter[0] += 1
            self._emit(marker)
            self.prefixes.append(" " * len(marker))
        elif tag == "blockquote":
            self._newline(blank=True)
            self.quotes.append(len(self.prefixes))
            self.prefixes.append("> ")
        elif tag == "pre":
            self._newline(blank=True)
            self.pre += 1
            if self.pre == 1:
                self.pre_parts = []
        elif tag == "a":
            href = dict(attrs).get("href")
            destination = _markdown_destination(href.strip()) if href is not None else None
            ok = destination is not None and link_allowed(href or "")
            self.links.append(destination if ok else None)
            if ok:
                self._text("[", raw=True)
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self._text(" ".join(alt.split()))
        elif tag == "table":
            self._newline(blank=True)
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.table.append([])
        elif tag in ("td", "th") and self.table is not None:
            if not self.table:
                self.table.append([])
            self.flush()
            self.cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID:
            self.depth = max(0, self.depth - 1)
        if tag in _DROP:
            if tag not in _VOID:
                self.skip = max(0, self.skip - 1)
            return
        if self.skip:
            return
        if tag in _INLINE:
            style = _INLINE[tag]
            if style in self.styles:
                del self.styles[len(self.styles) - 1 - self.styles[::-1].index(style)]
        elif tag == "code" and self.code and not self.pre:
            self.code -= 1
            self._text("`", raw=True)
        elif tag in _BLOCK or (len(tag) == 2 and tag[0] == "h" and tag[1] in "123456"):
            self._newline(blank=True)
        elif tag in ("ul", "ol"):
            if self.lists:
                self._close_containers(self.lists.pop()[1])
            self._newline(blank=True)
        elif tag == "li" and self.lists:
            self._close_containers(self.lists[-1][1])
        elif tag == "blockquote" and self.quotes:
            self._close_containers(self.quotes.pop())
            self._newline(blank=True)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            if self.pre == 0:
                self.finish_pre()
        elif tag == "a" and self.links:
            href = self.links.pop()
            if href is not None:
                self._text(f"]({href})", raw=True)
        elif tag in ("td", "th") and self.table is not None and self.cell is not None:
            self.flush()
            text = " ".join("".join(self.cell).split()).replace("|", "\\|")
            self.cell = None
            if self.table:
                self.table[-1].append(text)
        elif tag == "table" and self.table is not None:
            rows = [row for row in self.table if row]
            self.table = None
            self._emit_table(rows)

    def _emit_table(self, rows: list[list[str]]) -> None:
        if not rows:
            return
        width = max(len(row) for row in rows)
        padded = [row + [""] * (width - len(row)) for row in rows]
        lines = ["| " + " | ".join(padded[0]) + " |", "|" + "|".join(" --- " for _ in range(width)) + "|"]
        lines.extend("| " + " | ".join(row) + " |" for row in padded[1:])
        self._newline(blank=True)
        self._emit("\n".join(lines))
        self._newline(blank=True)

    def handle_data(self, data: str) -> None:
        if self.skip:
            return
        if self.pre:
            self._text(data)
        elif self.code:
            self._text(" ".join(data.split()) if data.strip() else data, raw=True)
        else:
            self._text(re.sub(r"\s+", " ", data))


def _markdown_destination(url: str) -> str | None:
    """A safe one-token Markdown destination for an already policy-checked HTML link."""
    if any(c in url for c in "<>\\"):
        return None
    return f"<{url}>" if any(c in url for c in "()") else url


def convert_html(html: str, inherited: tuple[str, ...] = ()) -> str | None:
    """Markdown for clipboard HTML, or None when it is too large or too deeply nested to convert.
    ``inherited`` names the styles (b, i, s) already open where the text goes, so the pasted text takes
    them on without markers of its own."""
    source = clipboard_fragment(html)
    if len(source) > MAX_HTML_CHARS:
        return None
    converter = _Converter(inherited)
    try:
        converter.feed(source)
        converter.close()
        converter.finish_pre()
        converter.flush()
    except _TooComplexError:
        return None
    text = "".join(converter.out)[:MAX_PASTE_CHARS]
    return re.sub(r"\n{3,}", "\n\n", text).strip("\n") + ("\n" if text.strip() else "")


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _DROP and tag not in _VOID:
            self.skip += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in _DROP and tag not in _VOID:
            self.skip = max(0, self.skip - 1)

    def handle_data(self, data: str) -> None:
        if not self.skip and len(self.parts) < MAX_HTML_ELEMENTS:
            self.parts.append(data)


def html_to_markdown(html: str) -> str:
    """Markdown for HTML; falls back to its escaped text when the HTML is too complex to convert."""
    converted = convert_html(html)
    if converted is not None:
        return converted
    reader = _TextOnly()
    reader.feed(clipboard_fragment(html)[:MAX_HTML_CHARS])
    reader.close()
    return escape_markdown(" ".join("".join(reader.parts).split()))[:MAX_PASTE_CHARS]


@dataclass(frozen=True, slots=True)
class DropPlan:
    open_paths: tuple[str, ...]
    ignored: int


def plan_drop(local_paths: list[str]) -> DropPlan:
    """Dropped files are opened as notes when they are notes; nothing is ever embedded."""
    notes = tuple(p for p in local_paths if ntpath.splitext(p)[1].lower() in NOTE_EXTENSIONS)
    return DropPlan(notes, len(local_paths) - len(notes))
