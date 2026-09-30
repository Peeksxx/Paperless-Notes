"""Paste and drop policy (SEC3, F09, ADR-0008).

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
from dataclasses import dataclass
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
    "b": "**",
    "strong": "**",
    "i": "*",
    "em": "*",
    "s": "~~",
    "strike": "~~",
    "del": "~~",
}
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
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.lists: list[list[int]] = []
        self.links: list[str | None] = []
        self.skip = 0
        self.pre = 0
        self.pre_parts: list[str] | None = None
        self.code = 0
        self.depth = 0
        self.elements = 0
        self.table: list[list[str]] | None = None
        self.cell: list[str] | None = None

    def _emit(self, text: str) -> None:
        if self.skip:
            return
        if self.pre and self.pre_parts is not None:
            self.pre_parts.append(text)
            return
        if self.cell is not None:
            self.cell.append(text)
        else:
            self.out.append(text)

    def _newline(self, blank: bool = False) -> None:
        if self.skip or self.cell is not None:
            return
        tail = "".join(self.out[-2:])
        if tail and not tail.endswith("\n"):
            self.out.append("\n")
        if blank and self.out and not "".join(self.out[-2:]).endswith("\n\n"):
            self.out.append("\n")

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
            self._emit(_INLINE[tag])
        elif tag == "u":
            self._emit("<u>")
        elif tag == "code" and not self.pre:
            self.code += 1
            self._emit("`")
        elif tag == "br":
            self._emit(" " if self.cell is not None else "\n")
        elif tag in _BLOCK:
            self._newline(blank=True)
        elif len(tag) == 2 and tag[0] == "h" and tag[1] in "123456":
            self._newline(blank=True)
            self._emit("#" * int(tag[1]) + " ")
        elif tag in ("ul", "ol"):
            self._newline()
            self.lists.append([1 if tag == "ol" else 0])
        elif tag == "li":
            self._newline()
            depth = max(len(self.lists) - 1, 0)
            counter = self.lists[-1] if self.lists else [0]
            marker = f"{counter[0]}. " if counter[0] else "- "
            if counter[0]:
                counter[0] += 1
            self._emit("  " * depth + marker)
        elif tag == "blockquote":
            self._newline(blank=True)
            self._emit("> ")
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
                self._emit("[")
        elif tag == "img":
            alt = dict(attrs).get("alt")
            if alt:
                self._emit(escape_markdown(" ".join(alt.split())))
        elif tag == "table":
            self._newline(blank=True)
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.table.append([])
        elif tag in ("td", "th") and self.table is not None:
            if not self.table:
                self.table.append([])
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
            self._emit(_INLINE[tag])
        elif tag == "u":
            self._emit("</u>")
        elif tag == "code" and self.code and not self.pre:
            self.code -= 1
            self._emit("`")
        elif tag in _BLOCK or (len(tag) == 2 and tag[0] == "h" and tag[1] in "123456"):
            self._newline(blank=True)
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._newline(blank=True)
        elif tag == "pre":
            self.pre = max(0, self.pre - 1)
            if self.pre == 0:
                self.finish_pre()
        elif tag == "a" and self.links:
            href = self.links.pop()
            if href is not None:
                self._emit(f"]({href})")
        elif tag in ("td", "th") and self.table is not None and self.cell is not None:
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
            self._emit(data)
        elif self.code:
            self._emit(" ".join(data.split()) if data.strip() else data)
        else:
            self._emit(escape_markdown(re.sub(r"\s+", " ", data)))


def _markdown_destination(url: str) -> str | None:
    """A safe one-token Markdown destination for an already policy-checked HTML link."""
    if any(c in url for c in "<>\\"):
        return None
    return f"<{url}>" if any(c in url for c in "()") else url


def convert_html(html: str) -> str | None:
    """Markdown for clipboard HTML, or None when it is too large or too deeply nested to convert."""
    source = clipboard_fragment(html)
    if len(source) > MAX_HTML_CHARS:
        return None
    converter = _Converter()
    try:
        converter.feed(source)
        converter.close()
        converter.finish_pre()
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
