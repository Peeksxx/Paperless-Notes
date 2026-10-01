"""Markdown rendered for export and rich copy: one renderer, one safety policy.

The note is read with the same lexer the editor uses and turned into a small block model (headings,
paragraphs, lists, task lists, quotes, fenced code, tables, rules). ``render_html`` writes inert HTML
from it and ``render_plain`` readable text without Markdown markers. Output is a projection: the note is
never changed. Raw HTML from the note never becomes markup (tags are dropped and their text escaped),
links survive only when the link policy allows them, and images appear only when the injected image
policy returns a source for them; otherwise their alt text is shown. Nothing here reads files.
"""

from __future__ import annotations

import html
import re
from collections.abc import Callable
from dataclasses import dataclass, field

from paperless_notes.mdio.lexer import (
    Span,
    State,
    Style,
    flatten,
    inline_spans,
    is_blank,
    is_table_delimiter,
    lex_line,
)
from paperless_notes.mdio.tables import split_row

MAX_DEPTH = 12
_LIST = re.compile(r"( {0,3})([-+*]|\d{1,9}[.)])([ \t]+|$)")
_TASK = re.compile(r"\[([ xX])\](?:[ \t]+|$)")
_QUOTE = re.compile(r" {0,3}> ?")
_TAG = re.compile(r"<[^<>]{0,2000}>")
_ATX_CLOSE = re.compile(r"[ \t]+#+[ \t]*$")
_BOX = "\N{BALLOT BOX}"
_CHECKED = "\N{BALLOT BOX WITH CHECK}"
_BULLET = "\N{BULLET}"
_RULE_TEXT = "\N{BOX DRAWINGS LIGHT HORIZONTAL}" * 24


@dataclass(frozen=True, slots=True)
class RenderPolicy:
    """``link(url)`` says whether a link may be kept; ``image(reference, alt)`` returns the ``src`` to
    write for an allowed, available image, or None to show its alt text instead."""

    link: Callable[[str], bool]
    image: Callable[[str, str], str | None] = lambda _ref, _alt: None


@dataclass
class Block:
    kind: str
    text: str = ""
    level: int = 0
    lines: list[str] = field(default_factory=list)
    children: list[Block] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    ordered: bool = False
    start: int = 1
    rows: list[list[str]] = field(default_factory=list)
    aligns: list[str] = field(default_factory=list)


@dataclass
class Item:
    task: bool | None
    blocks: list[Block]


def _indent_width(line: str) -> int:
    width = 0
    for ch in line:
        if ch == " ":
            width += 1
        elif ch == "\t":
            width += 4 - width % 4
        else:
            break
    return width


def _dedent(line: str, amount: int) -> str:
    removed = 0
    i = 0
    while i < len(line) and removed < amount and line[i] in " \t":
        removed += 1 if line[i] == " " else 4 - removed % 4
        i += 1
    return line[i:]


def _starts_block(line: str, following: str | None) -> bool:
    """A line that ends a paragraph: heading, fence, rule, quote, list item, table or HTML block."""
    if is_blank(line):
        return True
    result = lex_line(line, State.NORMAL, False, None)
    if result.heading or State.fence_info(result.state) is not None or result.state != State.NORMAL:
        return True
    styles = Style.NONE
    for span in result.spans:
        if span.start == 0 and span.end >= len(line):
            styles |= span.style
    if styles & (Style.RULE | Style.HTML):
        return True
    if _QUOTE.match(line) or _LIST.match(line):
        return True
    return following is not None and "|" in line and is_table_delimiter(following) and "|" in following


def parse_blocks(lines: list[str], top: bool = True, depth: int = 0) -> list[Block]:
    """The block structure of ``lines`` (one note, or the inside of a quote or list item)."""
    blocks: list[Block] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        following = lines[i + 1] if i + 1 < n else None
        if is_blank(line):
            i += 1
            continue
        result = lex_line(line, State.NORMAL, top and i == 0, following)
        if result.state == State.FRONT_MATTER:
            i += 1
            state = State.FRONT_MATTER
            while i < n and state == State.FRONT_MATTER:
                state = lex_line(lines[i], state).state
                i += 1
            continue
        fence = State.fence_info(result.state)
        if fence is not None:
            info = line.strip().lstrip(fence[0]).strip().split(" ", 1)[0]
            body: list[str] = []
            state = result.state
            i += 1
            while i < n:
                state = lex_line(lines[i], state).state
                if State.fence_info(state) is None:
                    i += 1
                    break
                body.append(_dedent(lines[i], fence[2]))
                i += 1
            blocks.append(Block("code", text=info, lines=body))
            continue
        if result.state == State.HTML_COMMENT:
            state = result.state
            i += 1
            while i < n and state == State.HTML_COMMENT:
                state = lex_line(lines[i], state).state
                i += 1
            continue
        if (
            result.heading
            and following is not None
            and _is_setext(following)
            and not line.lstrip().startswith("#")
        ):
            blocks.append(Block("heading", text=line.strip(), level=result.heading))
            i += 2
            continue
        if result.heading:
            content = line.lstrip(" ")[result.heading :]
            close = _ATX_CLOSE.search(content)
            if close:
                content = content[: close.start()]
            blocks.append(Block("heading", text=content.strip(), level=result.heading))
            i += 1
            continue
        whole = Style.NONE
        for span in result.spans:
            if span.start == 0 and span.end >= len(line):
                whole |= span.style
        if whole & Style.RULE:
            blocks.append(Block("rule"))
            i += 1
            continue
        if following is not None and "|" in line and is_table_delimiter(following) and "|" in following:
            i = _table(lines, i, blocks)
            continue
        if whole & Style.HTML:
            text = "" if line.lstrip().startswith("<!--") else _TAG.sub("", line).strip()
            if text:
                blocks.append(Block("paragraph", lines=[html.unescape(text)]))
            i += 1
            continue
        if _QUOTE.match(line) and depth < MAX_DEPTH:
            i = _quote(lines, i, blocks, depth)
            continue
        if _LIST.match(line) and depth < MAX_DEPTH:
            i = _list(lines, i, blocks, depth)
            continue
        paragraph = [line.lstrip()]
        i += 1
        while i < n and not _starts_block(lines[i], lines[i + 1] if i + 1 < n else None):
            if i + 1 < n and _is_setext(lines[i + 1]):
                break
            paragraph.append(lines[i].lstrip())
            i += 1
        blocks.append(Block("paragraph", lines=paragraph))
    return blocks


def _is_setext(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and len(line) - len(line.lstrip(" ")) <= 3 and set(stripped) in ({"="}, {"-"})


def _table(lines: list[str], i: int, blocks: list[Block]) -> int:
    header = [c.strip() for c in split_row(lines[i]).cells]
    aligns: list[str] = []
    for cell in split_row(lines[i + 1]).cells:
        c = cell.strip()
        aligns.append(
            "center" if c.startswith(":") and c.endswith(":") else "right" if c.endswith(":") else ""
        )
    rows: list[list[str]] = []
    i += 2
    while i < len(lines) and "|" in lines[i] and not is_blank(lines[i]):
        rows.append([c.strip() for c in split_row(lines[i]).cells])
        i += 1
    blocks.append(Block("table", rows=[header, *rows], aligns=aligns))
    return i


def _quote(lines: list[str], i: int, blocks: list[Block], depth: int) -> int:
    inner: list[str] = []
    while i < len(lines):
        line = lines[i]
        m = _QUOTE.match(line)
        if m:
            inner.append(line[m.end() :])
        elif inner and not is_blank(line) and not is_blank(inner[-1]) and not _starts_block(line, None):
            inner.append(line)
        else:
            break
        i += 1
    blocks.append(Block("quote", children=parse_blocks(inner, False, depth + 1)))
    return i


def _list(lines: list[str], i: int, blocks: list[Block], depth: int) -> int:
    first = _LIST.match(lines[i])
    if first is None:
        return i + 1
    ordered = first.group(2)[0].isdigit()
    start = int(first.group(2)[:-1]) if ordered else 1
    delimiter = first.group(2)[-1]
    items: list[Item] = []
    while i < len(lines):
        m = _LIST.match(lines[i])
        if m is None or m.group(2)[0].isdigit() != ordered or m.group(2)[-1] != delimiter:
            break
        content_indent = len(m.group(1)) + len(m.group(2)) + (len(m.group(3)) if m.group(3) else 1)
        rest = lines[i][m.end() :]
        task: bool | None = None
        t = _TASK.match(rest)
        if t:
            task = t.group(1) in "xX"
            rest = rest[t.end() :]
        body = [rest]
        i += 1
        while i < len(lines):
            line = lines[i]
            if is_blank(line):
                if i + 1 < len(lines) and _indent_width(lines[i + 1]) >= content_indent:
                    body.append("")
                    i += 1
                    continue
                break
            if _indent_width(line) >= content_indent:
                body.append(_dedent(line, content_indent))
            elif not _starts_block(line, None) and body and not is_blank(body[-1]):
                body.append(line.strip())
            else:
                break
            i += 1
        items.append(Item(task, parse_blocks(body, False, depth + 1)))
        if i < len(lines) and is_blank(lines[i]):
            j = i
            while j < len(lines) and is_blank(lines[j]):
                j += 1
            nxt = _LIST.match(lines[j]) if j < len(lines) else None
            if nxt is None or nxt.group(2)[0].isdigit() != ordered or len(nxt.group(1)) > len(first.group(1)):
                break
            i = j
    blocks.append(Block("list", items=items, ordered=ordered, start=start))
    return i


@dataclass(frozen=True, slots=True)
class _Link:
    start: int
    end: int
    target: str | None
    image: bool


def _links(text: str, spans: list[Span]) -> list[_Link]:
    found: list[_Link] = []
    for span in spans:
        if span.style & Style.LINK and not span.style & Style.MARKER:
            image = span.start >= 2 and text[span.start - 2 : span.start] == "!["
            target = None
            for other in spans:
                if other.start == span.end + 1 and other.style & Style.URL and other.style & Style.MARKER:
                    raw = text[other.start : other.end].strip()
                    raw = raw[1:-1].strip() if raw.startswith("(") and raw.endswith(")") else raw
                    if raw.startswith("<") and ">" in raw:
                        target = raw[1 : raw.index(">")]
                    else:
                        target = raw.split(" ", 1)[0]
                    break
            found.append(_Link(span.start, span.end, target or None, image))
        elif span.style & Style.URL and not span.style & Style.MARKER:
            target = text[span.start : span.end].strip("<>")
            found.append(_Link(span.start, span.end, target, False))
    return found


def _hidden(text: str, spans: list[Span]) -> bytearray:
    """Characters that are Markdown syntax, not content: escape backslashes, markers, link destinations,
    raw HTML tags and the angle brackets of autolinks."""
    hidden = bytearray(len(text))
    for span in spans:
        if span.style & Style.MARKER and not span.style & Style.HARD_BREAK:
            for p in range(max(0, span.start), min(len(text), span.end)):
                hidden[p] = 1
        elif (
            span.style & Style.URL and text[span.start : span.start + 1] == "<" and span.end - 1 > span.start
        ):
            hidden[span.start] = hidden[span.end - 1] = 1
    return hidden


def _inline_parts(text: str) -> list[tuple[str, int, _Link | None]]:
    """(text, style bits, link) runs of one line of inline Markdown, markers removed."""
    spans = inline_spans(text)
    links = _links(text, spans)
    hidden = _hidden(text, spans)
    for link in links:
        if link.image:
            for p in range(link.start - 2, min(len(text), link.end + 1)):
                hidden[p] = 1
    bits = [0] * len(text)
    for start, end, style in flatten(spans, len(text)):
        for p in range(start, end):
            bits[p] = style
    parts: list[tuple[str, int, _Link | None]] = []
    images = {link.start: link for link in links if link.image}
    starts = {link.start for link in links}
    i = 0
    while i < len(text):
        image = images.get(i + 2)
        if image is not None and text[i : i + 2] == "![":
            parts.append((text[image.start : image.end], -1, image))
            i = image.end
            while i < len(text) and hidden[i]:
                i += 1
            continue
        if hidden[i]:
            i += 1
            continue
        owner = next((lk for lk in links if lk.start <= i < lk.end and not lk.image), None)
        j = i + 1
        while j < len(text) and not hidden[j] and bits[j] == bits[i] and j not in starts:
            if owner is not None and j >= owner.end:
                break
            j += 1
        parts.append((text[i:j], bits[i], owner))
        i = j
    return parts


_WRAPPERS = (
    (Style.CODE, "code"),
    (Style.STRONG, "strong"),
    (Style.EMPHASIS, "em"),
    (Style.STRIKE, "s"),
    (Style.UNDERLINE, "u"),
)


def _inline_html(text: str, policy: RenderPolicy) -> str:
    out: list[str] = []
    for part, bits, link in _inline_parts(text):
        if bits == -1 and link is not None:
            src = policy.image(link.target or "", part) if link.target else None
            if src is not None:
                out.append(f'<img src="{html.escape(src)}" alt="{html.escape(part)}">')
            else:
                out.append(f'<span class="image">[Image: {html.escape(part or "no description")}]</span>')
            continue
        piece = html.escape(part, quote=False)
        for style, name in _WRAPPERS:
            if bits & style:
                piece = f"<{name}>{piece}</{name}>"
        if link is not None and link.target and policy.link(link.target):
            piece = f'<a href="{html.escape(link.target)}">{piece}</a>'
        out.append(piece)
    return "".join(out)


def _inline_plain(text: str) -> str:
    """Inline text without markers; a link keeps its label followed by its target in parentheses."""
    out: list[str] = []
    current: _Link | None = None
    label: list[str] = []

    def finish() -> None:
        if current is not None and current.target and "".join(label) != current.target:
            out.append(f" ({current.target})")

    for part, bits, link in _inline_parts(text):
        if bits == -1:
            finish()
            current, label = None, []
            out.append(f"[Image: {part or 'no description'}]")
            continue
        if link is not current:
            finish()
            current, label = link, []
        out.append(part)
        label.append(part)
    finish()
    return "".join(out)


def _paragraph_html(lines: list[str], policy: RenderPolicy) -> str:
    rendered: list[str] = []
    for index, line in enumerate(lines):
        hard = line.endswith(("  ", "\\"))
        body = line.rstrip(" ")
        if body.endswith("\\"):
            body = body[:-1]
        rendered.append(_inline_html(body, policy))
        if index < len(lines) - 1:
            rendered.append("<br>\n" if hard else "\n")
    return "".join(rendered)


def blocks_html(blocks: list[Block], policy: RenderPolicy, tight: bool = False) -> str:
    out: list[str] = []
    for block in blocks:
        if block.kind == "heading":
            level = min(max(block.level, 1), 6)
            out.append(f"<h{level}>{_inline_html(block.text, policy)}</h{level}>")
        elif block.kind == "paragraph":
            inner = _paragraph_html(block.lines, policy)
            out.append(inner if tight else f"<p>{inner}</p>")
        elif block.kind == "text":
            out.append(f"<p>{html.escape(' '.join(block.lines), quote=False)}</p>")
        elif block.kind == "code":
            language = f' class="language-{html.escape(block.text)}"' if block.text else ""
            code = html.escape("\n".join(block.lines), quote=False)
            out.append(f"<pre><code{language}>{code}</code></pre>")
        elif block.kind == "rule":
            out.append("<hr>")
        elif block.kind == "quote":
            out.append(f"<blockquote>{blocks_html(block.children, policy)}</blockquote>")
        elif block.kind == "list":
            out.append(_list_html(block, policy))
        elif block.kind == "table":
            out.append(_table_html(block, policy))
    return "\n".join(out)


def _list_html(block: Block, policy: RenderPolicy) -> str:
    tag = "ol" if block.ordered else "ul"
    start = f' start="{block.start}"' if block.ordered and block.start != 1 else ""
    items: list[str] = []
    for item in block.items:
        body = blocks_html(item.blocks, policy, tight=True)
        if item.task is not None:
            if item.task:
                items.append(f'<li class="task">{_CHECKED} <span class="done">{body}</span></li>')
            else:
                items.append(f'<li class="task">{_BOX} {body}</li>')
        else:
            items.append(f"<li>{body}</li>")
    return f"<{tag}{start}>\n" + "\n".join(items) + f"\n</{tag}>"


def _table_html(block: Block, policy: RenderPolicy) -> str:
    def cell(tag: str, text: str, column: int) -> str:
        align = block.aligns[column] if column < len(block.aligns) else ""
        style = f' style="text-align: {align}"' if align else ""
        return f"<{tag}{style}>{_inline_html(text, policy)}</{tag}>"

    header, *rows = block.rows
    head = "".join(cell("th", text, c) for c, text in enumerate(header))
    body = "\n".join("<tr>" + "".join(cell("td", t, c) for c, t in enumerate(r)) + "</tr>" for r in rows)
    return f"<table>\n<thead><tr>{head}</tr></thead>\n<tbody>\n{body}\n</tbody>\n</table>"


def render_html(text: str, policy: RenderPolicy) -> str:
    """The note as an HTML fragment: inert markup only, deterministic for the same text and policy."""
    return blocks_html(parse_blocks(text.split("\n")), policy)


def blocks_plain(blocks: list[Block], indent: str = "", tight: bool = False) -> list[str]:
    out: list[str] = []
    for block in blocks:
        if out and out[-1] and not (tight and block.kind == "list"):
            out.append("")
        if block.kind == "heading":
            out.append(indent + _inline_plain(block.text))
        elif block.kind == "paragraph":
            out += [indent + _inline_plain(line.rstrip(" \\")) for line in block.lines]
        elif block.kind == "text":
            out.append(indent + " ".join(block.lines))
        elif block.kind == "code":
            out += [indent + line for line in block.lines]
        elif block.kind == "rule":
            out.append(indent + _RULE_TEXT)
        elif block.kind == "quote":
            out += blocks_plain(block.children, indent + "    ")
        elif block.kind == "list":
            out += _list_plain(block, indent)
        elif block.kind == "table":
            out += _table_plain(block, indent)
    while out and not out[-1]:
        out.pop()
    return out


def _list_plain(block: Block, indent: str) -> list[str]:
    out: list[str] = []
    for number, item in enumerate(block.items, block.start):
        if item.task is not None:
            marker = (_CHECKED if item.task else _BOX) + " "
        elif block.ordered:
            marker = f"{number}. "
        else:
            marker = _BULLET + " "
        lines = blocks_plain(item.blocks, indent + " " * len(marker), tight=True)
        if lines:
            lines[0] = indent + marker + lines[0][len(indent) + len(marker) :]
        else:
            lines = [indent + marker.rstrip()]
        out += lines
    return out


def _table_plain(block: Block, indent: str) -> list[str]:
    rows = [[_inline_plain(c) for c in row] for row in block.rows]
    columns = max(len(r) for r in rows)
    widths = [min(40, max((len(r[c]) for r in rows if c < len(r)), default=0)) for c in range(columns)]
    lines: list[str] = []
    for row in rows:
        cells = [(row[c] if c < len(row) else "").ljust(widths[c]) for c in range(columns)]
        lines.append((indent + "  ".join(cells)).rstrip())
    return lines


def render_plain(text: str) -> str:
    """The note as readable text: markers, link syntax, front matter, comments and HTML tags removed."""
    lines = blocks_plain(parse_blocks(text.split("\n")))
    return "\n".join(lines) + ("\n" if lines else "")
