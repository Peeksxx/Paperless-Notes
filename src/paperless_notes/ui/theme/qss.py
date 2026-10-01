"""The application stylesheet, generated from one Theme. Widgets opt in through object names and the
``role``, ``kind`` and ``severity`` dynamic properties instead of carrying their own style strings. The tab
strip, caption buttons, row buttons, library rows, palette and dashboard are painted in code."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette

from paperless_notes.ui.theme.tokens import Theme


def _font_stack(families: tuple[str, ...]) -> str:
    return ", ".join(f'"{f}"' for f in families)


def build_stylesheet(theme: Theme) -> str:
    p = theme.palette
    t = theme.typography
    r = theme.radius
    s = theme.spacing
    m = theme.metrics
    ui = _font_stack(t.ui_families)
    mono = _font_stack(t.mono_families)
    display = _font_stack(t.display_families)
    return f"""
* {{ font-family: {ui}; font-size: {t.ui_pt}pt; }}
QWidget {{ color: {p.text}; }}
QMainWindow, QDialog, #AppRoot {{ background: {p.window}; }}
QToolTip {{
    background: {p.surface}; color: {p.text}; border: 1px solid {p.border_strong};
    border-radius: {r.sm}px; padding: {s.xs}px {s.sm}px;
}}

#AppBar {{ background: {p.window}; border-bottom: 1px solid {p.border}; }}
#AppBar QToolButton {{ min-width: {m.hit_min}px; min-height: {m.hit_min}px; padding: 0; }}
#AppBar QToolButton[kind="labelled"] {{ min-width: 0; padding: 0 {s.sm}px; color: {p.text_secondary}; }}
#AppBar QToolButton[kind="labelled"]:hover {{ color: {p.text}; }}
QLabel[role="crumb-current"] {{ color: {p.text}; padding-left: {s.xs}px; }}
QToolButton#CrumbOverflow::menu-indicator {{ image: none; width: 0px; }}

#Sidebar {{ background: {p.window}; border-right: 1px solid {p.border}; }}
QTreeView#LibraryTree {{ background: transparent; border: none; outline: 0; }}
#Sidebar QToolButton {{ min-width: 22px; min-height: 22px; padding: 1px; border-radius: {r.sm}px; }}

#TabStripHost {{ background: {p.window}; border-bottom: 1px solid {p.border}; }}
QTabBar#TabStrip {{ background: transparent; }}
QToolButton#TabClose {{
    padding: 0; min-width: 20px; min-height: 20px; border-radius: {r.sm}px; border: none;
}}
QToolButton#TabClose:hover {{ background: {p.selected}; }}
QToolButton#StripButton {{ min-width: {m.hit_min}px; min-height: {m.hit_min}px; padding: 0; }}
QTabBar QToolButton {{ background: {p.window}; border: none; border-radius: {r.sm}px; }}
QTabBar QToolButton:hover {{ background: {p.hover}; }}
QTabBar::tear {{ width: 0px; border: none; }}

#PageHeader, QScrollArea#PageHeaderScroll {{ background: {p.page}; border: none; }}
QPlainTextEdit#TitleField {{
    background: transparent; border: 1px solid transparent; border-radius: {r.md}px;
    font-family: {display}; font-size: {t.title_pt}pt; font-weight: 600; padding: 0px {s.xxs}px;
    color: {p.text}; selection-background-color: {p.selection}; selection-color: {p.selection_text};
}}
QPlainTextEdit#TitleField:hover {{ border-color: {p.border}; }}
QPlainTextEdit#TitleField:focus {{ border: 1px solid {p.focus}; }}
QLabel#SyncLine {{
    color: {p.text_secondary}; font-size: {t.meta_pt + 0.5}pt;
    padding-left: {s.xs + 1}px;
}}
QLabel#SyncLine[state="needs_review"], QLabel#SyncLine[state="offline_or_stalled"] {{
    color: {p.warning_text};
}}

#Banner {{ border-radius: {r.md}px; border: 1px solid {p.border_strong}; background: {p.surface}; }}
#Banner[severity="warn"] {{ background: {p.warning_background}; border-color: {p.warning_border}; }}
#Banner[severity="error"] {{ background: {p.error_background}; border-color: {p.error_border}; }}
#Banner QLabel#BannerTitle {{ font-weight: 600; }}
#Banner[severity="warn"] QLabel {{ color: {p.warning_text}; }}
#Banner[severity="error"] QLabel {{ color: {p.error_text}; }}

QPushButton {{
    background: {p.surface}; color: {p.text}; border: 1px solid {p.border_strong};
    border-radius: {r.md}px; padding: {s.xs}px {s.lg}px; min-height: {m.control - 10}px;
}}
QPushButton:hover {{ background: {p.hover}; }}
QPushButton:pressed {{ background: {p.selected}; }}
QPushButton:focus {{ border: 2px solid {p.focus}; padding: {s.xs - 1}px {s.lg - 1}px; }}
QPushButton:disabled {{ color: {p.text_muted}; border-color: {p.border}; }}
QPushButton[kind="primary"] {{ background: {p.accent}; color: {p.on_accent}; border-color: {p.accent}; }}
QPushButton[kind="primary"]:hover {{ background: {p.accent_hover}; border-color: {p.accent_hover}; }}
QPushButton[kind="primary"]:pressed {{ background: {p.accent_pressed}; }}
QPushButton[kind="primary"]:focus {{ border: 2px solid {p.text}; }}
QPushButton[kind="quiet"] {{ background: transparent; border-color: transparent; color: {p.text_secondary}; }}
QPushButton[kind="quiet"]:hover {{ background: {p.hover}; color: {p.text}; }}
QPushButton[kind="quiet"]:focus {{ border: 2px solid {p.focus}; }}
QPushButton[kind="danger"] {{ color: {p.error_text}; border-color: {p.error_border}; }}

QToolButton {{
    background: transparent; border: 1px solid transparent; border-radius: {r.md}px;
    padding: {s.xs}px; min-width: {m.hit_min - 10}px; min-height: {m.hit_min - 10}px;
    color: {p.text_secondary};
}}
QToolButton:hover {{ background: {p.hover}; color: {p.text}; }}
QToolButton:pressed, QToolButton:checked {{ background: {p.selected}; color: {p.text}; }}
QToolButton:focus {{ border: 1px solid {p.focus}; }}
QToolButton:disabled {{ color: {p.text_muted}; }}
QToolButton[kind="labelled"] {{ padding: {s.xs}px {s.md}px; }}
QToolButton::menu-indicator {{ image: none; width: 0px; }}

QLineEdit, QSpinBox, QComboBox {{
    background: {p.page}; border: 1px solid {p.border_strong}; border-radius: {r.md}px;
    padding: {s.xs}px {s.md}px; min-height: {m.control - 10}px; selection-background-color: {p.selection};
    selection-color: {p.selection_text};
}}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus {{
    border: 2px solid {p.focus};
    padding: {s.xs - 1}px {s.md - 1}px;
}}
QComboBox QAbstractItemView {{
    background: {p.surface}; border: 1px solid {p.border_strong}; selection-background-color: {p.selected};
    selection-color: {p.text};
}}
QCheckBox {{ spacing: {s.md}px; }}
QCheckBox:focus {{ color: {p.text}; text-decoration: underline; }}

QLineEdit#PaletteField {{
    background: transparent; border: none; padding: 0 {s.xs}px; font-size: {t.ui_pt + 2.5}pt;
    min-height: 40px; selection-background-color: {p.selection}; selection-color: {p.selection_text};
}}
QLineEdit#PaletteField:focus {{ border: none; padding: 0 {s.xs}px; }}
QListView#PaletteList {{ background: transparent; border: none; outline: 0; }}
QLabel#PaletteStatus {{ color: {p.text_muted}; font-size: {t.meta_pt}pt; }}
QLabel#PaletteStatus a {{ color: {p.text_secondary}; }}

QMenu {{
    background: {p.surface};
    border: 1px solid {p.border_strong};
    padding: {s.xs}px;
}}
QMenu::item {{ padding: {s.sm}px {s.xxl}px {s.sm}px {s.md}px; border-radius: {r.sm}px; }}
QMenu::item:selected {{ background: {p.hover}; color: {p.text}; }}
QMenu::item:disabled {{ color: {p.text_muted}; }}
QMenu::separator {{ height: 1px; background: {p.border}; margin: {s.xs}px {s.md}px; }}
QMenu::indicator {{ width: 12px; height: 12px; }}

#StatusBar {{
    background: {p.window};
    border-top: 1px solid {p.border};
    color: {p.text_muted};
}}
#StatusBar QLabel {{
    color: {p.text_muted}; font-size: {t.meta_pt}pt; padding: 0 {s.md}px;
}}
#StatusBar QToolButton {{
    font-size: {t.meta_pt}pt; padding: 0 {s.sm}px; min-height: {m.status_bar - 6}px;
    min-width: {m.hit_min}px; color: {p.text_muted};
}}

QLabel[role="secondary"] {{ color: {p.text_secondary}; }}
QLabel[role="muted"] {{ color: {p.text_muted}; font-size: {t.small_pt}pt; }}
QLabel[role="heading"] {{ font-family: {display}; font-size: {t.ui_pt + 4}pt; font-weight: 600; }}
QLabel[role="section"] {{
    color: {p.text_muted}; font-size: {t.meta_pt}pt; font-weight: 600;
}}
QLabel[role="meta"] {{ color: {p.text_muted}; font-size: {t.meta_pt}pt; }}

#Toast, #Hint {{ background: {p.surface}; border: 1px solid {p.border_strong}; border-radius: {r.lg}px; }}
#Panel {{ background: {p.page}; border-left: 1px solid {p.border}; }}
#PaneFrame {{ background: {p.page}; border: none; border-top: 2px solid {p.window}; }}
#PaneFrame[active="true"] {{ border-top: 2px solid {p.accent}; }}
#PaneHeader {{ background: {p.window}; border-bottom: 1px solid {p.border}; }}
#Dashboard, #DashboardBody, #PageArea {{ background: {p.page}; }}
QLabel#HomeDate {{ color: {p.text_muted}; font-size: {t.meta_pt}pt; font-weight: 600; }}
QLabel#HomeHeading {{
    color: {p.text}; font-family: {display}; font-size: {t.display_pt}pt; font-weight: 600;
}}
QFrame#BarDivider {{ background: {p.border}; border: none; }}
QScrollArea#DashboardScroll {{ background: {p.page}; border: none; }}
QListWidget[role="plain"] {{ background: transparent; border: none; outline: 0; }}
QListWidget[role="plain"]::item {{ min-height: {m.control}px; border-radius: {r.sm}px; padding: 0 {s.sm}px; }}
QListWidget[role="plain"]::item:hover {{ background: {p.hover}; }}
QListWidget[role="plain"]::item:selected {{ background: {p.selected}; color: {p.text}; }}
QTextBrowser[role="plain"] {{ background: transparent; border: none; }}

QPlainTextEdit#DiffPane, QPlainTextEdit#MergeResult {{
    font-family: {mono}; background: {p.page}; border: 1px solid {p.border}; border-radius: {r.md}px;
}}
QSlider::groove:horizontal {{ height: 4px; background: {p.selected}; border-radius: 2px; }}
QSlider::handle:horizontal {{
    background: {p.accent}; width: 14px; height: 14px; margin: -6px 0; border-radius: 7px;
    border: 2px solid {p.page};
}}
QSlider:focus::handle:horizontal {{ border: 2px solid {p.focus}; }}

QScrollBar:vertical {{ background: transparent; width: 10px; margin: 0; }}
QScrollBar::handle:vertical {{
    background: {p.selected};
    border-radius: {r.sm}px;
    min-height: 32px;
    margin: 2px 2px 2px 3px;
}}
QScrollBar::handle:vertical:hover {{ background: {p.border_strong}; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 0; }}
QScrollBar::handle:horizontal {{
    background: {p.selected};
    border-radius: {r.sm}px;
    min-width: 32px;
    margin: 3px 2px 2px 2px;
}}
QScrollBar::handle:horizontal:hover {{ background: {p.border_strong}; }}
QScrollBar::add-line, QScrollBar::sub-line, QScrollBar::add-page, QScrollBar::sub-page {{
    background: transparent; border: none; width: 0; height: 0;
}}
QSplitter::handle {{ background: {p.border}; }}
"""


def build_palette(theme: Theme) -> QPalette:
    p = theme.palette
    palette = QPalette()
    roles = {
        QPalette.ColorRole.Window: p.window,
        QPalette.ColorRole.WindowText: p.text,
        QPalette.ColorRole.Base: p.page,
        QPalette.ColorRole.AlternateBase: p.window,
        QPalette.ColorRole.Text: p.text,
        QPalette.ColorRole.Button: p.surface,
        QPalette.ColorRole.ButtonText: p.text,
        QPalette.ColorRole.Highlight: p.selection,
        QPalette.ColorRole.HighlightedText: p.selection_text,
        QPalette.ColorRole.ToolTipBase: p.surface,
        QPalette.ColorRole.ToolTipText: p.text,
        QPalette.ColorRole.PlaceholderText: p.text_muted,
        QPalette.ColorRole.Link: p.link,
        QPalette.ColorRole.BrightText: p.text,
        QPalette.ColorRole.Light: p.surface,
        QPalette.ColorRole.Midlight: p.hover,
        QPalette.ColorRole.Mid: p.border,
        QPalette.ColorRole.Dark: p.border_strong,
        QPalette.ColorRole.Shadow: p.border_strong,
    }
    for role, color in roles.items():
        palette.setColor(role, QColor(color))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Text, QColor(p.text_muted))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText, QColor(p.text_muted))
    palette.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText, QColor(p.text_muted))
    return palette
