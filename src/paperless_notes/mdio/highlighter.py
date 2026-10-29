"""Live styling of Markdown source: markers dimmed or concealed, headings larger, emphasis rendered.

The highlighter only sets layout formats and block states. It never edits the document, so it adds no
undo steps and cannot change the text. With concealment on, every line except the caret line
hides its eligible markers: inline markers shrink to zero width, block markers (bullets, task boxes,
quote marks, rules) keep their width but become transparent so the editor can paint a symbol in place.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field, replace

from PySide6.QtCore import QTimer
from PySide6.QtGui import (
    QColor,
    QFont,
    QSyntaxHighlighter,
    QTextBlock,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)

from paperless_notes.mdio.lexer import (
    MAX_PARAGRAPH_LINES,
    LineResult,
    ParagraphRole,
    Span,
    State,
    Style,
    continues,
    flatten,
    is_blank,
    is_setext_underline,
    lex_line,
    may_span_lines,
    paragraph_inline,
    paragraph_role,
)

HEADING_SCALE = (1.0, 1.6, 1.35, 1.2, 1.1, 1.0, 1.0)
_KEEP_LINE = Style.TABLE | Style.CODE_BLOCK | Style.META
_KEEP_SPAN = Style.HTML | Style.HARD_BREAK | Style.FOOTNOTE | Style.LINK | Style.TABLE | Style.META
SPELL_SKIP = Style.CODE | Style.CODE_BLOCK | Style.URL | Style.HTML | Style.META | Style.FOOTNOTE
_TRANSPARENT = QColor(0, 0, 0, 0)
_ID_SHIFT = 12
_LEXER_MASK = (1 << _ID_SHIFT) - 1
_MAX_ID = (1 << (31 - _ID_SHIFT)) - 1
_CACHE_LIMIT = 4096
_HEADING_BIT = int(Style.HEADING)
_LexKey = tuple[str, int, bool, str | None]


SpellCheck = Callable[[str, list[tuple[int, int]]], list[tuple[int, int]]]


def utf16_offsets(text: str) -> list[int] | None:
    """Document offsets (UTF-16 code units) of every Python index of ``text`` and its end, or None when
    they are the same: only characters outside the Basic Multilingual Plane, such as most emoji, take two
    units."""
    if text.isascii() or max(text) <= "\uffff":
        return None
    offsets = [0]
    for ch in text:
        offsets.append(offsets[-1] + (2 if ch > "\uffff" else 1))
    return offsets


def _decor_in_units(decor: BlockDecor, units: list[int]) -> BlockDecor:
    return replace(
        decor,
        bullets=tuple(units[i] for i in decor.bullets),
        tasks=tuple((units[i], checked) for i, checked in decor.tasks),
        quotes=tuple(units[i] for i in decor.quotes),
        concealed=tuple((units[a], units[b]) for a, b in decor.concealed),
        task_bullets=tuple((units[a], units[b]) for a, b in decor.task_bullets),
    )


@dataclass(slots=True)
class _Paragraph:
    """The lines of one paragraph and their inline styles worked out together. ``lines`` is None when each
    line keeps its own inline styles (nothing can run across lines), and ``long`` marks a paragraph too long
    to work out as a whole. ``blocks`` is the note's line count when this was worked out."""

    first: int
    texts: list[str]
    lines: list[list[Span]] | None
    heading: int
    blocks: int
    long: bool = False

    @property
    def last(self) -> int:
        return self.first + len(self.texts) - 1


def _signature(result: LineResult) -> int:
    return hash((tuple(result.spans), result.heading, result.state))


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
    spelling: str = "#e5484d"


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
    task_bullets: tuple[tuple[int, int], ...] = field(default=())


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
        self._active: QTextCursor | None = None
        self._thresholds: tuple[int, ...] = ()
        self.highlighted_blocks = 0
        self._decor: dict[int, tuple[int, BlockDecor]] = {}
        self._free: list[int] = []
        self._next_id = 1
        self._spell: SpellCheck | None = None
        self._caret = (-1, -1)
        self._skipped_block = -1
        self._lexed: dict[_LexKey, LineResult] = {}
        self._joined: dict[tuple[str, ...], list[list[Span]] | None] = {}
        self._signatures: dict[int, int] = {}
        self._paras: dict[int, _Paragraph] = {}
        self._dirty: list[tuple[int, int]] = []
        self._verify_pending = False
        document.contentsChange.connect(self._on_contents_change)

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

    def set_spelling(self, check: SpellCheck | None) -> None:
        """Underline the words ``check`` reports as misspelled; None turns underlining off."""
        self._spell = check
        self.rehighlight()

    def spelling_changed(self) -> None:
        if self._spell is not None:
            self.rehighlight()

    def set_caret(self, block: int, column: int) -> None:
        """The caret position. The word being typed at the caret is not underlined yet; when the caret
        leaves it on the same line, the line is checked again. On the caret line a bullet, task box or
        quote bar shows once the caret is past its marker, so moving across a marker restyles the line."""
        old_column = self._caret[1]
        same_line = block == self._caret[0]
        self._caret = (block, column)
        crossed = any((old_column > t) != (column > t) for t in self._thresholds)
        if same_line and (block == self._skipped_block or (crossed and self._conceal)):
            found = self.document().findBlockByNumber(block)
            if found.isValid():
                self.rehighlightBlock(found)

    def active_block(self) -> QTextBlock | None:
        """The caret line, followed through edits above it (a cursor keeps its place as text changes)."""
        if self._active is None:
            return None
        block = self._active.block()
        return block if block.isValid() else None

    def set_active_block(self, number: int) -> None:
        """The caret line shows its markers; only the old and new caret lines are re-styled."""
        previous = self.active_block()
        if previous is not None and previous.blockNumber() == number:
            return
        block = self.document().findBlockByNumber(number) if number >= 0 else QTextBlock()
        self._active = QTextCursor(block) if block.isValid() else None
        if previous is None and not block.isValid():
            return
        if not self._conceal:
            return
        for target in (previous, block):
            if target is not None and target.isValid():
                self._forget(target.blockNumber() - 1)
                self.rehighlightBlock(target)

    def _is_active(self, block: QTextBlock) -> bool:
        active = self.active_block()
        return active is not None and active == block

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
            self._signatures.pop(key, None)
        self._free.extend(dead)

    def highlightBlock(self, text: str) -> None:
        self.highlighted_blocks += 1
        block = self.currentBlock()
        prev = lexer_state(self.previousBlockState())
        result = self._analyse(block, text, prev if prev >= 0 else State.NORMAL)
        block_id = self._block_id()
        self.setCurrentBlockState(result.state | (block_id << _ID_SHIFT))
        if block_id:
            self._signatures[block_id] = _signature(result)
        units = utf16_offsets(text)
        for start, end, style in flatten(result.spans, len(text)):
            if units is not None:
                start, end = units[start], units[end]
            self.setFormat(start, end - start, self._format(style, result.heading))
        decor = self._decorate(text, result)
        if units is not None:
            decor = _decor_in_units(decor, units)
        length = units[-1] if units is not None else len(text)
        if self._conceal and not self._is_active(block):
            self._apply_conceal(decor, length)
        elif self._conceal:
            decor = self._caret_line(decor)
            self._apply_conceal(decor, length, caret_line=True)
        else:
            decor = replace(decor, bullets=(), tasks=(), quotes=(), rule=False, concealed=(), task_bullets=())
        if block_id:
            self._decor[block_id] = (length, decor)
        if self._spell is not None and result.state != State.FRONT_MATTER:
            self._underline_misspelled(block.blockNumber(), text, result, units)
        self._check_previous(block, text)

    def _underline_misspelled(
        self, number: int, text: str, result: LineResult, units: list[int] | None
    ) -> None:
        check = self._spell
        if check is None:
            return
        skip = [(span.start, span.end) for span in result.spans if span.style & SPELL_SKIP]
        ranges = check(text, skip)
        if units is not None:
            ranges = [(units[a], units[b]) for a, b in ranges]
        caret_block, caret_column = self._caret
        if number == caret_block:
            kept = [(a, b) for a, b in ranges if not a < caret_column <= b]
            if len(kept) != len(ranges):
                self._skipped_block = number
            ranges = kept
        color = QColor(self._theme.spelling)
        for start, end in ranges:
            for position in range(start, end):
                underlined = QTextCharFormat(self.format(position))
                underlined.setUnderlineStyle(QTextCharFormat.UnderlineStyle.SpellCheckUnderline)
                underlined.setUnderlineColor(color)
                self.setFormat(position, 1, underlined)

    def _check_previous(self, block: QTextBlock, text: str) -> None:
        """Restyle the line above when this line changes its meaning: a ``---`` or ``===`` underline makes
        it a heading, and the second line decides whether a first-line ``---`` opens front matter."""
        previous = block.previous()
        if not previous.isValid() or is_blank(previous.text()):
            return
        number = previous.blockNumber()
        stored = self.decor_for(previous)
        stored_heading = stored.heading if stored is not None else 0
        if number != 0 and not is_setext_underline(text) and not stored_heading:
            return
        if not self._current(previous):
            target = QTextCursor(previous)
            QTimer.singleShot(0, self, lambda: self._restyle(target))

    def _state_before(self, block: QTextBlock) -> int:
        before = block.previous()
        state = lexer_state(before.userState()) if before.isValid() else State.NORMAL
        return state if state >= 0 else State.NORMAL

    def _current(self, block: QTextBlock) -> bool:
        """Whether ``block`` is styled the way its text and its neighbours call for now."""
        state = block.userState()
        if state < 0 or not state >> _ID_SHIFT:
            return False
        expected = _signature(self._analyse(block, block.text(), self._state_before(block)))
        return self._signatures.get(state >> _ID_SHIFT) == expected

    def _lex(self, text: str, state: int, first: bool, following: str | None) -> LineResult:
        key = (text, state, first, following)
        found = self._lexed.get(key)
        if found is None:
            if len(self._lexed) > _CACHE_LIMIT:
                self._lexed.clear()
            found = self._lexed[key] = lex_line(text, state, first, following)
        return found

    def _analyse(self, block: QTextBlock, text: str, state: int) -> LineResult:
        """The line's styling: the line on its own, then its inline styles worked out across the whole
        paragraph it belongs to (bold or code can run over a line break), and a paragraph of several lines
        above a ``---`` or ``===`` underline becomes one heading."""
        result = self._lex(text, state, block.blockNumber() == 0, self._lookahead(block.next()))
        role = paragraph_role(text, result)
        if role is None:
            return result
        found = self._paragraph(block, text, result, role)
        if found is None:
            return result
        spans, heading = found
        if spans is None and heading == result.heading:
            return result
        head = [s for s in result.spans[: result.block_spans] if not int(s.style) & _HEADING_BIT]
        if heading:
            head.insert(0, Span(0, len(text), Style.HEADING))
        base = result.inline_from
        if spans is None:
            body = result.spans[result.block_spans :]
        else:
            body = [Span(s.start + base, s.end + base, s.style) for s in spans]
        return LineResult([*head, *body], result.state, heading, base, len(head))

    def _paragraph(
        self, block: QTextBlock, text: str, result: LineResult, role: ParagraphRole
    ) -> tuple[list[Span] | None, int] | None:
        number = block.blockNumber()
        known = self._known(number, text)
        if known is not None:
            if known.long:
                return None
            spans = None if known.lines is None else known.lines[number - known.first]
            return spans, known.heading
        above: list[tuple[str, LineResult, ParagraphRole]] = []
        current, current_role = block, role
        while not current_role.item:
            previous = current.previous()
            if not previous.isValid():
                break
            previous_text = previous.text()
            found = self._lex(
                previous_text,
                self._state_before(previous),
                previous.blockNumber() == 0,
                self._lookahead(current),
            )
            found_role = paragraph_role(previous_text, found)
            if found_role is None or found.heading or not continues(found_role, current_role):
                break
            long = self._known(previous.blockNumber(), previous_text)
            if not above and long is not None and long.long and long.last == number - 1:
                long.texts.append(text)
                self._paras[number] = long
                return None
            if len(above) >= MAX_PARAGRAPH_LINES:
                self._remember(
                    [*(t for t, _, _ in reversed(above)), text], number - len(above), None, 0, True
                )
                return None
            above.append((previous_text, found, found_role))
            current, current_role = previous, found_role
        below: list[tuple[str, LineResult, ParagraphRole]] = []
        current, current_result, current_role = block, result, role
        while not current_result.heading:
            following = current.next()
            if not following.isValid():
                break
            following_text = following.text()
            found = self._lex(following_text, current_result.state, False, self._lookahead(following.next()))
            found_role = paragraph_role(following_text, found)
            if found_role is None or not continues(current_role, found_role):
                break
            below.append((following_text, found, found_role))
            current, current_result, current_role = following, found, found_role
            if len(above) + len(below) >= MAX_PARAGRAPH_LINES:
                texts = [*(t for t, _, _ in reversed(above)), text, *(t for t, _, _ in below)]
                self._remember(texts, number - len(above), None, 0, True)
                return None
        if not above and not below:
            return None
        lines = [(text, result, role) for text, result, role in reversed(above)]
        lines.append((text, result, role))
        lines.extend(below)
        first = lines[0][2]
        heading = 0 if first.item or first.quoted else lines[-1][1].heading
        contents = tuple(line[found.inline_from :] for line, found, _ in lines)
        joined: list[list[Span]] | None = None
        if may_span_lines(contents):
            if contents in self._joined:
                joined = self._joined[contents]
            else:
                if len(self._joined) > _CACHE_LIMIT // 8:
                    self._joined.clear()
                joined = self._joined[contents] = paragraph_inline(contents)
        self._remember([line for line, _, _ in lines], number - len(above), joined, heading)
        return (None if joined is None else joined[len(above)]), heading

    def _known(self, number: int, text: str) -> _Paragraph | None:
        """The remembered paragraph of line ``number``, if the note still has the same lines there."""
        found = self._paras.get(number)
        if found is None or found.blocks != self._block_count():
            return None
        index = number - found.first
        return found if 0 <= index < len(found.texts) and found.texts[index] == text else None

    def _remember(
        self, texts: list[str], first: int, lines: list[list[Span]] | None, heading: int, long: bool = False
    ) -> None:
        if len(self._paras) > _CACHE_LIMIT + 4 * self._block_count():
            self._paras.clear()
        found = _Paragraph(first, texts, lines, heading, self._block_count(), long)
        for number in range(first, first + len(texts)):
            self._paras[number] = found

    def _forget(self, number: int) -> None:
        found = self._paras.get(number)
        if found is None:
            return
        for other in range(found.first, found.first + len(found.texts)):
            if self._paras.get(other) is found:
                del self._paras[other]

    def _block_count(self) -> int:
        document = self.document()
        return document.blockCount() if document is not None else 0

    def _on_contents_change(self, position: int, _removed: int, added: int) -> None:
        document = self.document()
        if document is not None:
            first = document.findBlock(position).blockNumber()
            last = document.findBlock(position + added).blockNumber()
            if first < 0:
                first = document.blockCount() - 1
            if last < 0:
                last = document.blockCount() - 1
            for number in range(first - 1, last + 2):
                self._forget(number)
        self._dirty.append((position, position + added))
        if not self._verify_pending:
            self._verify_pending = True
            QTimer.singleShot(0, self, self._verify)

    def _verify(self) -> None:
        """After an edit, restyle the lines around it whose styling depends on the edited text (a closing
        ``**`` two lines down, a paragraph that split or joined). The edited lines themselves were styled
        with the edit."""
        self._verify_pending = False
        dirty, self._dirty = self._dirty, []
        document = self.document()
        if document is None:
            return
        for start, end in dirty[-16:]:
            first = document.findBlock(start)
            last = document.findBlock(end)
            if not first.isValid():
                first = document.lastBlock()
            if not last.isValid():
                last = document.lastBlock()
            for step in (QTextBlock.previous, QTextBlock.next):
                block = step(first if step is QTextBlock.previous else last)
                for _ in range(MAX_PARAGRAPH_LINES + 1):
                    if not block.isValid():
                        break
                    if not self._current(block):
                        self.rehighlightBlock(block)
                    if self._role(block) is None:
                        break
                    block = step(block)

    def _role(self, block: QTextBlock) -> ParagraphRole | None:
        text = block.text()
        found = self._lex(
            text, self._state_before(block), block.blockNumber() == 0, self._lookahead(block.next())
        )
        return paragraph_role(text, found)

    def _lookahead(self, following: QTextBlock) -> str | None:
        """The line after the one being styled. A lone ``-`` on the caret line is still being typed (it
        usually becomes a list item), so it does not turn the line above into a heading until the caret
        leaves it."""
        if not following.isValid():
            return None
        text = following.text()
        if self._conceal and text.strip() == "-" and self._is_active(following):
            return ""
        return text

    def _caret_line(self, decor: BlockDecor) -> BlockDecor:
        """The caret line keeps its markers as typed text, except a bullet, task box or quote bar the caret
        has moved past: those are drawn at once, the same as on other lines."""
        column = self._caret[1]
        bullets = tuple(p for p in decor.bullets if column > p + 1)
        quotes = tuple(p for p in decor.quotes if column > p + 1)
        tasks = tuple((p, checked) for p, checked in decor.tasks if column > p + 3)
        shown = {p for p, _ in tasks}
        hidden = tuple((a, b) for a, b in decor.task_bullets if any(a < p for p in shown))
        marks = {p + 1 for p in decor.bullets} | {p + 1 for p in decor.quotes}
        self._thresholds = tuple(sorted(marks | {p + 3 for p, _ in decor.tasks}))
        return replace(
            decor, bullets=bullets, quotes=quotes, tasks=tasks, rule=False, concealed=(), task_bullets=hidden
        )

    def _restyle(self, target: QTextCursor) -> None:
        """A cursor rather than a block: it stays valid through edits and turns null if the note closes."""
        if target.isNull() or target.document() is not self.document():
            return
        block = target.block()
        if block.isValid():
            self.rehighlightBlock(block)

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
        task_bullets: list[tuple[int, int]] = []
        kinds = [(span, self._marker_kind(text, span, result.heading)) for span in result.spans]
        has_task = any(kind == "task" for _, kind in kinds)
        for span, kind in kinds:
            if kind == "rule":
                rule = True
            elif kind == "bullet" and has_task:
                end = span.end + (1 if text[span.end : span.end + 1] == " " else 0)
                task_bullets.append((span.start, end))
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
            task_bullets=tuple(task_bullets),
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

    def _apply_conceal(self, decor: BlockDecor, length: int, caret_line: bool = False) -> None:
        zero = QTextCharFormat()
        zero.setFontPointSize(0.01)
        zero.setForeground(_TRANSPARENT)
        for start, end in decor.concealed:
            self.setFormat(start, end - start, zero)
        if not caret_line:
            for start, end in decor.task_bullets:
                self.setFormat(start, end - start, zero)
        hidden_positions = [(p, 1) for p in decor.bullets] + [(p, 1) for p in decor.quotes]
        hidden_positions += [(p, 3) for p, _ in decor.tasks]
        if caret_line:
            hidden_positions += [(a, b - a) for a, b in decor.task_bullets]
        for pos, length in hidden_positions:
            fmt = QTextCharFormat(self.format(pos))
            fmt.setForeground(_TRANSPARENT)
            self.setFormat(pos, length, fmt)
        if decor.rule:
            fmt = QTextCharFormat(self.format(0))
            fmt.setForeground(_TRANSPARENT)
            self.setFormat(0, length, fmt)

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
