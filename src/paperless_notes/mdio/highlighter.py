"""Live styling of Markdown source: markers dimmed or concealed, headings larger, emphasis rendered.

The highlighter only sets layout formats and block states. It never edits the document, so it adds no
undo steps and cannot change the text (ADR-0007). With concealment on, every line except the caret line
hides its eligible markers: inline markers shrink to zero width, block markers (bullets, task boxes,
quote marks, rules) keep their width but become transparent so the editor can paint a symbol in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from PySide6.QtGui import (
    QColor,
    QFont,
    QSyntaxHighlighter,
    QTextBlock,
    QTextCharFormat,
    QTextDocument,
)

from paperless_notes.mdio.lexer import LineResult, Span, State, Style, flatten, lex_line

HEADING_SCALE = (1.0, 1.6, 1.35, 1.2, 1.1, 1.0, 1.0)
_KEEP_LINE = Style.TABLE | Style.CODE_BLOCK | Style.META
_KEEP_SPAN = Style.HTML | Style.HARD_BREAK | Style.FOOTNOTE | Style.LINK | Style.TABLE | Style.META
_TRANSPARENT = QColor(0, 0, 0, 0)
_ID_SHIFT = 12
_LEXER_MASK = (1 << _ID_SHIFT) - 1
_MAX_ID = (1 << (31 - _ID_SHIFT)) - 1


def lexer_state(state: int) -> int:
    """The lexer part of a block state; the bits above it number the block's decorations."""
    return state & _LEXER_MASK if state >= 0 else -1


@dataclass(frozen=True, slots=True)
class HighlightTheme:
    text: str = "#e6eef6"
    marker: str = "#64748b"
    muted: str = "#94a3b8"
    accent: str = "#38bdf8"
    link: str = "#38bdf8"
    code_background: str = "#16213a"
    quote: str = "#cbd5e1"
    html: str = "#94a3b8"
    base_point_size: float = 13.0
    body_families: tuple[str, ...] = ()
    mono_family: str = "Cascadia Mono"


@dataclass(frozen=True, slots=True)
class BlockDecor:
    """What the editor paints for one line. Positions are offsets within the line."""

    heading: int = 0
    bullets: tuple[int, ...] = ()
    tasks: tuple[tuple[int, bool], ...] = ()
    quotes: tuple[int, ...] = ()
    rule: bool = False
    code: bool = False
    table: bool = False
    concealed: tuple[tuple[int, int], ...] = field(default=())


def block_decor(block: QTextBlock) -> BlockDecor | None:
    """Decorations of ``block`` from the MarkdownHighlighter attached to its document, if any."""
    document = block.document()
    if document is None:
        return None
    for child in document.children():
        if isinstance(child, MarkdownHighlighter):
            return child.decor_for(block)
    return None


class MarkdownHighlighter(QSyntaxHighlighter):
    """Highlights one block at a time; state carries fences, front matter and tables across lines."""

    def __init__(
        self, document: QTextDocument, theme: HighlightTheme | None = None, conceal: bool = False
    ) -> None:
        super().__init__(document)
        self._theme = theme or HighlightTheme()
        self._cache: dict[tuple[int, int], QTextCharFormat] = {}
        self._conceal = conceal
        self._active = -1
        self.highlighted_blocks = 0
        self._decor: dict[int, tuple[int, BlockDecor]] = {}
        self._free: list[int] = []
        self._next_id = 1

    @property
    def theme(self) -> HighlightTheme:
        return self._theme

    @property
    def conceal(self) -> bool:
        return self._conceal

    def set_theme(self, theme: HighlightTheme) -> None:
        """Restyle the document; nothing happens when the theme is unchanged (a second view of the same
        note applies the same theme)."""
        if theme == self._theme:
            return
        self._theme = theme
        self._cache.clear()
        self.rehighlight()

    def set_base_point_size(self, size: float) -> None:
        self.set_theme(replace(self._theme, base_point_size=size))

    def set_conceal(self, conceal: bool) -> None:
        if conceal != self._conceal:
            self._conceal = conceal
            self.rehighlight()

    def set_active_block(self, number: int) -> None:
        """The caret line shows its markers; only the old and new caret lines are re-styled."""
        if number == self._active:
            return
        previous, self._active = self._active, number
        if not self._conceal:
            return
        document = self.document()
        for n in (previous, number):
            block = document.findBlockByNumber(n) if n >= 0 else None
            if block is not None and block.isValid():
                self.rehighlightBlock(block)

    def decor_for(self, block: QTextBlock) -> BlockDecor | None:
        """What to paint for ``block``; None until the block is highlighted again after a change.

        Decorations live here, keyed by a number in the block state, rather than in block user data:
        PySide keeps every user data object alive once Python has read it, which leaked one object per
        painted line (measured by tools/ui_memcheck.py).
        """
        state = block.userState()
        entry = self._decor.get(state >> _ID_SHIFT) if state >= 0 else None
        if entry is None or entry[0] != block.length() - 1:
            return None
        return entry[1]

    def _block_id(self) -> int:
        current = self.currentBlockState()
        if current >= 0 and current >> _ID_SHIFT:
            return current >> _ID_SHIFT
        document = self.document()
        if not self._free and document is not None and len(self._decor) > 2 * document.blockCount() + 256:
            self._collect(document)
        if self._free:
            return self._free.pop()
        if self._next_id > _MAX_ID:
            return 0
        self._next_id += 1
        return self._next_id - 1

    def _collect(self, document: QTextDocument) -> None:
        """Forget decorations of blocks that no longer exist and reuse their numbers."""
        live: set[int] = set()
        block = document.firstBlock()
        while block.isValid():
            state = block.userState()
            if state >= 0:
                live.add(state >> _ID_SHIFT)
            block = block.next()
        dead = [key for key in self._decor if key not in live]
        for key in dead:
            self._decor.pop(key)
        self._free.extend(dead)

    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt override
        self.highlighted_blocks += 1
        block = self.currentBlock()
        following = block.next()
        next_text = following.text() if following.isValid() else None
        prev = lexer_state(self.previousBlockState())
        result: LineResult = lex_line(
            text, prev if prev >= 0 else State.NORMAL, block.blockNumber() == 0, next_text
        )
        block_id = self._block_id()
        self.setCurrentBlockState(result.state | (block_id << _ID_SHIFT))
        for start, end, style in flatten(result.spans, len(text)):
            self.setFormat(start, end - start, self._format(style, result.heading))
        decor = self._decorate(text, result)
        if self._conceal and block.blockNumber() != self._active:
            self._apply_conceal(decor)
        else:
            decor = replace(decor, bullets=(), tasks=(), quotes=(), rule=False, concealed=())
        if block_id:
            self._decor[block_id] = (len(text), decor)

    def _decorate(self, text: str, result: LineResult) -> BlockDecor:
        line_style = Style.NONE
        for span in result.spans:
            line_style |= span.style
        decor = BlockDecor(
            heading=result.heading,
            code=bool(line_style & Style.CODE_BLOCK),
            table=bool(line_style & Style.TABLE),
        )
        if line_style & _KEEP_LINE:
            return decor
        bullets: list[int] = []
        tasks: list[tuple[int, bool]] = []
        quotes: list[int] = []
        rule = False
        hidden: list[tuple[int, int]] = []
        kinds = [(span, self._marker_kind(text, span, result.heading)) for span in result.spans]
        has_task = any(kind == "task" for _, kind in kinds)
        for span, kind in kinds:
            if kind == "rule":
                rule = True
            elif kind == "bullet" and has_task:
                end = span.end + (1 if text[span.end : span.end + 1] == " " else 0)
                hidden.append((span.start, end))
            elif kind == "bullet":
                bullets.append(span.start)
            elif kind == "task":
                tasks.append((span.start, text[span.start + 1 : span.start + 2] in ("x", "X")))
            elif kind == "quote":
                quotes.append(span.start)
            elif kind == "zero":
                end = span.end
                if result.heading and span.start == 0 and text[end : end + 1] == " ":
                    end += 1
                hidden.append((span.start, end))
        return replace(
            decor,
            bullets=tuple(bullets),
            tasks=tuple(tasks),
            quotes=tuple(quotes),
            rule=rule,
            concealed=tuple(hidden),
        )

    @staticmethod
    def _marker_kind(text: str, span: Span, heading: int) -> str:
        style = span.style
        if not style & Style.MARKER or style & _KEEP_SPAN:
            return ""
        segment = text[span.start : span.end]
        if not segment:
            return ""
        if style & Style.RULE:
            return "rule"
        if style & Style.LIST:
            if segment.startswith("["):
                return "task"
            return "" if segment[0].isdigit() else "bullet"
        if segment == ">" and not heading:
            return "quote"
        return "zero"

    def _apply_conceal(self, decor: BlockDecor) -> None:
        zero = QTextCharFormat()
        zero.setFontPointSize(0.01)
        zero.setForeground(_TRANSPARENT)
        for start, end in decor.concealed:
            self.setFormat(start, end - start, zero)
        hidden_positions = [(p, 1) for p in decor.bullets] + [(p, 1) for p in decor.quotes]
        hidden_positions += [(p, 3) for p, _ in decor.tasks]
        for pos, length in hidden_positions:
            fmt = QTextCharFormat(self.format(pos))
            fmt.setForeground(_TRANSPARENT)
            self.setFormat(pos, length, fmt)
        if decor.rule:
            fmt = QTextCharFormat(self.format(0))
            fmt.setForeground(_TRANSPARENT)
            self.setFormat(0, len(self.currentBlock().text()), fmt)

    def _format(self, bits: int, heading: int) -> QTextCharFormat:
        key = (bits, heading)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        style = Style(bits)
        t = self._theme
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(t.text))
        if style & Style.HEADING or heading:
            fmt.setFontWeight(QFont.Weight.Bold)
            fmt.setFontPointSize(t.base_point_size * HEADING_SCALE[min(max(heading, 1), 6)])
        if style & Style.STRONG:
            fmt.setFontWeight(QFont.Weight.Bold)
        if style & Style.EMPHASIS:
            fmt.setFontItalic(True)
        if style & Style.STRIKE:
            fmt.setFontStrikeOut(True)
        if style & Style.UNDERLINE:
            fmt.setFontUnderline(True)
        if style & (Style.CODE | Style.CODE_BLOCK | Style.META | Style.TABLE):
            fmt.setFontFamilies([t.mono_family, "Consolas", "Courier New"])
        if style & Style.CODE and not style & Style.CODE_BLOCK:
            fmt.setBackground(QColor(t.code_background))
        if style & Style.QUOTE:
            fmt.setForeground(QColor(t.quote))
            fmt.setFontItalic(True)
        if style & (Style.LINK | Style.URL | Style.FOOTNOTE):
            fmt.setForeground(QColor(t.link))
            fmt.setFontUnderline(bool(style & (Style.LINK | Style.URL)))
        if style & Style.TASK_DONE:
            fmt.setForeground(QColor(t.muted))
            fmt.setFontStrikeOut(True)
        if style & Style.HTML:
            fmt.setForeground(QColor(t.html))
        if style & Style.HARD_BREAK:
            fmt.setBackground(QColor(t.code_background))
        if style & (Style.MARKER | Style.RULE):
            fmt.setForeground(QColor(t.marker))
            fmt.setFontUnderline(False)
        if style & Style.LIST and style & Style.MARKER:
            fmt.setForeground(QColor(t.muted))
        self._cache[key] = fmt
        return fmt
