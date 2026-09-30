"""Design tokens (docs/DESIGN.md): graphite neutrals, state colours, type, spacing, radius, motion.

The interface and content use Segoe UI Variable; captions, paths, times, counts, shortcuts and sync state
use its small optical size. Cascadia Mono is kept for code, diffs and the Mono note face. Saturated colour
marks state only. Colours are defined per theme and checked for contrast by the tests (4.5:1 for text,
3:1 for markers, state dots, focus rings and control edges).
"""

from __future__ import annotations

from dataclasses import dataclass

LIGHT = "light"
DARK = "dark"
SYSTEM = "system"
MODES = (SYSTEM, LIGHT, DARK)
GRAPHITE = "graphite"
LIME = "lime"
ACCENTS = (GRAPHITE, LIME)


@dataclass(frozen=True, slots=True)
class Palette:
    name: str
    window: str
    page: str
    surface: str
    hover: str
    selected: str
    border: str
    border_strong: str
    text: str
    text_secondary: str
    text_muted: str
    marker: str
    accent: str
    accent_hover: str
    accent_pressed: str
    on_accent: str
    focus: str
    selection: str
    selection_text: str
    link: str
    code_background: str
    quote: str
    warning_text: str
    warning_background: str
    warning_border: str
    error_text: str
    error_background: str
    error_border: str
    diff_add_text: str
    diff_add_background: str
    diff_remove_text: str
    diff_remove_background: str
    changed_mark: str
    find_match: str
    find_current: str
    state_ok: str
    state_busy: str
    state_attention: str
    danger_fill: str
    danger_pressed: str
    on_danger: str
    shadow: str


_LIGHT_BASE = {
    "window": "#f3f4f6",
    "page": "#ffffff",
    "surface": "#ffffff",
    "hover": "#eaecef",
    "selected": "#e0e3e8",
    "border": "#e2e4e8",
    "border_strong": "#858b94",
    "text": "#16181c",
    "text_secondary": "#4b515a",
    "text_muted": "#596069",
    "marker": "#858b94",
    "selection": "#d3dbe6",
    "selection_text": "#16181c",
    "code_background": "#f1f3f5",
    "quote": "#4a5058",
    "warning_text": "#7a4f00",
    "warning_background": "#fff6e0",
    "warning_border": "#a47f26",
    "error_text": "#a8201a",
    "error_background": "#fdeeed",
    "error_border": "#c9463d",
    "diff_add_text": "#1d6b39",
    "diff_add_background": "#e7f5ec",
    "diff_remove_text": "#a8201a",
    "diff_remove_background": "#fdeeed",
    "changed_mark": "#6d7a8a",
    "find_match": "#f9e8b0",
    "find_current": "#eec95c",
    "state_ok": "#23824a",
    "state_busy": "#a8660b",
    "state_attention": "#c9463d",
    "danger_fill": "#c42b1c",
    "danger_pressed": "#a82419",
    "on_danger": "#ffffff",
    "shadow": "#0b0d10",
}

_DARK_BASE = {
    "window": "#121317",
    "page": "#17181c",
    "surface": "#1d1f23",
    "hover": "#25272c",
    "selected": "#2d3036",
    "border": "#27292f",
    "border_strong": "#686e78",
    "text": "#e7e8eb",
    "text_secondary": "#aab0b8",
    "text_muted": "#979da6",
    "marker": "#71777f",
    "selection": "#34404f",
    "selection_text": "#f1f3f5",
    "code_background": "#1f2126",
    "quote": "#b7bcc4",
    "warning_text": "#f0c36a",
    "warning_background": "#2b2415",
    "warning_border": "#8c7234",
    "error_text": "#ff8f86",
    "error_background": "#2e1b1b",
    "error_border": "#bd544c",
    "diff_add_text": "#8fd4a5",
    "diff_add_background": "#173022",
    "diff_remove_text": "#ff9a92",
    "diff_remove_background": "#3a1f1f",
    "changed_mark": "#8c9aab",
    "find_match": "#40361a",
    "find_current": "#6b5518",
    "state_ok": "#5cc389",
    "state_busy": "#e3a948",
    "state_attention": "#ff8f86",
    "danger_fill": "#c42b1c",
    "danger_pressed": "#a82419",
    "on_danger": "#ffffff",
    "shadow": "#000000",
}

_ACCENT_VALUES = {
    (GRAPHITE, LIGHT): ("#2b2f36", "#3b4048", "#1d2025", "#ffffff"),
    (GRAPHITE, DARK): ("#d6dae0", "#e4e7eb", "#c3c8cf", "#16181b"),
    (LIME, LIGHT): ("#4d6d0b", "#5a7d10", "#3f5a08", "#ffffff"),
    (LIME, DARK): ("#aeda2e", "#bde345", "#9cc424", "#16181b"),
}


def palette(mode: str, accent: str = GRAPHITE) -> Palette:
    """The resolved palette for ``light`` or ``dark`` and one of ``ACCENTS``."""
    base = _DARK_BASE if mode == DARK else _LIGHT_BASE
    fill, hover, pressed, on_fill = _ACCENT_VALUES[(accent if accent in ACCENTS else GRAPHITE, mode)]
    return Palette(
        name=DARK if mode == DARK else LIGHT,
        accent=fill,
        accent_hover=hover,
        accent_pressed=pressed,
        on_accent=on_fill,
        focus=fill,
        link=fill,
        **base,
    )


@dataclass(frozen=True, slots=True)
class Typography:
    ui_families: tuple[str, ...] = ("Segoe UI Variable", "Segoe UI")
    display_families: tuple[str, ...] = ("Segoe UI Variable Display", "Segoe UI Variable", "Segoe UI")
    serif_families: tuple[str, ...] = ("Georgia",)
    mono_families: tuple[str, ...] = ("Cascadia Mono", "Consolas")
    ui_pt: float = 9.5
    small_pt: float = 8.75
    caption_pt: float = 8.25
    meta_pt: float = 8.25
    title_pt: float = 22.0
    display_pt: float = 24.0
    note_pt: float = 12.0
    text_opsz: float = 10.5
    display_opsz: float = 36.0
    meta_opsz: float = 8.0


NOTE_FONTS = {
    "sans": Typography().ui_families,
    "serif": Typography().serif_families,
    "mono": Typography().mono_families,
}


@dataclass(frozen=True, slots=True)
class Spacing:
    xxs: int = 2
    xs: int = 4
    sm: int = 6
    md: int = 8
    lg: int = 12
    xl: int = 16
    xxl: int = 24
    xxxl: int = 32


@dataclass(frozen=True, slots=True)
class Radius:
    sm: int = 4
    md: int = 6
    lg: int = 10


@dataclass(frozen=True, slots=True)
class Metrics:
    title_bar: int = 44
    caption_button: int = 46
    search_box: int = 30
    tab_bar: int = 34
    status_bar: int = 26
    control: int = 28
    row: int = 30
    icon: int = 16
    hit_min: int = 28
    sidebar: int = 264
    sidebar_min: int = 208
    sidebar_max: int = 420
    readable_width: int = 720
    dashboard_width: int = 1040
    palette_width: int = 640
    gutter: int = 28


@dataclass(frozen=True, slots=True)
class Motion:
    fast_ms: int = 120
    normal_ms: int = 180
    palette_ms: int = 140
    toast_ms: int = 4000

    def duration(self, ms: int, reduced: bool) -> int:
        return 0 if reduced else ms


@dataclass(frozen=True, slots=True)
class Theme:
    palette: Palette
    typography: Typography = Typography()
    spacing: Spacing = Spacing()
    radius: Radius = Radius()
    metrics: Metrics = Metrics()
    motion: Motion = Motion()
    reduced_motion: bool = False

    @property
    def dark(self) -> bool:
        return self.palette.name == DARK

    def ms(self, value: int) -> int:
        return self.motion.duration(value, self.reduced_motion)


def theme(mode: str, accent: str = GRAPHITE, reduced_motion: bool = False) -> Theme:
    return Theme(palette(mode, accent), reduced_motion=reduced_motion)


def _channel(value: int) -> float:
    c = value / 255
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def luminance(color: str) -> float:
    """WCAG relative luminance of ``#rrggbb``."""
    r, g, b = (int(color[i : i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast(foreground: str, background: str) -> float:
    a, b = sorted((luminance(foreground), luminance(background)), reverse=True)
    return (a + 0.05) / (b + 0.05)
