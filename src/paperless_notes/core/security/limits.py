"""Input limits in one place, so settings, the session and the editor agree."""

from __future__ import annotations

from dataclasses import dataclass

from paperless_notes.core.textformat import DEFAULT_MAX_BYTES

MAX_INLINE_CHARS = 10_000
MAX_BRACKET_DEPTH = 32
MAX_QUOTE_DEPTH = 16


@dataclass(frozen=True, slots=True)
class InputLimits:
    max_file_bytes: int = DEFAULT_MAX_BYTES
    max_inline_chars: int = MAX_INLINE_CHARS
    max_quote_depth: int = MAX_QUOTE_DEPTH
    max_bracket_depth: int = MAX_BRACKET_DEPTH
    max_paste_chars: int = 5_000_000
    highlight_max_bytes: int = 512 * 1024

    def highlighting_enabled(self, size_bytes: int) -> bool:
        """Very large notes open as plain source so the UI stays responsive."""
        return size_bytes <= self.highlight_max_bytes
