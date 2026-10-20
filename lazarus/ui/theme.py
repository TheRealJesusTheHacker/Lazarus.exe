"""The Lazarus look: first light — warm stone-black, bone, and
ember amber, and nothing purple, ever.

Lazarus's own face, from the fleet namesake palette program
(2026-10-20): resurrection at dawn. The window sits on warm
stone-black, the words are soft bone-white, and the primary —
headings, accents, the main buttons — is ember amber, the first
light coming back; quiet information headings take the pale dawn
accent. Before v1.0.2 the window wore the shared Sentinel green;
the engines underneath did not change, only the colours did.

Every colour the window uses lives here, by name, so the contrast
of every text/background pairing the window actually uses can be
checked in one place (``PAIRINGS`` below, pinned by
``tests/test_ui_theme.py`` at 4.5:1 or better — the WCAG AA floor
for ordinary text). Danger is the fleet red (#b03a2e, white
words) — the same red every Sentinel tool uses — kept for the
destructive final actions (running a decryptor, applying a
tidy-up) and, in its lighter reading tone, for problem headings;
it is never a background for body text and never decoration.
"""

from __future__ import annotations

# -- the palette -------------------------------------------------------
#
# Namesake palette (fleet program, 2026-10-20): warm stone-black
# base, bone text, ember amber primary ("first light"), pale dawn
# accent. Measured contrast ratios (WCAG) are noted per colour;
# tests/test_ui_theme.py recomputes and pins every pairing in
# PAIRINGS below.

BG = "#0d0a07"  # the window itself: warm stone-black
PANEL = "#171209"  # panels and cards sitting on the window
PANEL_RAISED = "#120d08"  # the inset tier (inputs, banners): the
# palette's input background — inputs sit IN the surface
TEXT = "#f2ead9"  # the words people read: soft bone-white
#   16.50 on BG · 15.57 on PANEL · 16.15 on PANEL_RAISED
TEXT_FAINT = "#b3a58c"  # quieter words
#   8.16 on BG · 7.70 on PANEL · 7.98 on PANEL_RAISED
TEXT_DISABLED = "#9a8c6f"  # disabled words — its own colour, not a
# faded copy of the faint: chosen to clear 4.5:1 on every surface
#   5.97 on BG · 5.64 on PANEL · 5.85 on PANEL_RAISED
GREEN = "#f0a13c"  # the primary: ember amber, "first light" —
# headings, accents, primary buttons. GREEN is the name the
# stylesheet and the odds chips have read since v1.0.0; the
# colour under it is Lazarus's own now (see PRIMARY below).
#   9.28 on BG · 8.76 on PANEL · 9.08 on PANEL_RAISED
GREEN_DARK = "#4d3416"  # selection background behind TEXT:
# GREEN mixed 28% over BG (derived from the pair, pinned)
#   TEXT on it: 9.65
GREEN_ON_DARK_TEXT = "#1a0f02"  # the words on a GREEN button: 8.87
ACCENT = "#d8c391"  # pale dawn — quiet information headings
#   11.41 on BG · 10.77 on PANEL · 11.17 on PANEL_RAISED
BORDER = "#33291a"  # hairlines between panels (not text; no ratio owed)
WHITE = "#ffffff"  # the words on the red accent: 6.02 on RED
RED = "#b03a2e"  # the fleet danger red, shared by every Sentinel
# tool: accent background for the destructive final actions
RED_TEXT = "#ff8a7a"  # problem headings, on BG/PANEL only — the
# fleet red's lighter reading tone: 8.62 on BG · 8.14 on PANEL

# Spec-name aliases — the namesake palette spec names these roles
# PRIMARY / PRIMARY_DARK_TEXT / INPUT_BG / PANEL_BORDER /
# DISABLED_TEXT; the aliases keep the spec and this module
# traceable to each other. Same colours, second names.
PRIMARY = GREEN
PRIMARY_DARK_TEXT = GREEN_ON_DARK_TEXT
SELECTION = GREEN_DARK
INPUT_BG = PANEL_RAISED
PANEL_BORDER = BORDER
DISABLED_TEXT = TEXT_DISABLED

#: Every (foreground, background, where-it-is-used) pairing the
#: window relies on. The theme test computes each one and refuses
#: anything under 4.5:1.
PAIRINGS: list[tuple[str, str, str]] = [
    (TEXT, BG, "body text on the window"),
    (TEXT, PANEL, "body text on a panel"),
    (TEXT, PANEL_RAISED, "body text on a raised panel or input"),
    (TEXT, GREEN_DARK, "selected list and tree rows"),
    (TEXT, RED, "words on a destructive-action button"),
    (TEXT_FAINT, BG, "quiet text on the window"),
    (TEXT_FAINT, PANEL, "quiet text on a panel"),
    (TEXT_FAINT, PANEL_RAISED, "quiet text on a raised panel"),
    (TEXT_DISABLED, BG, "disabled text on the window"),
    (TEXT_DISABLED, PANEL, "disabled text on a panel"),
    (TEXT_DISABLED, PANEL_RAISED, "disabled text on a raised panel"),
    (GREEN, BG, "headings and accents on the window"),
    (GREEN, PANEL, "headings and accents on a panel"),
    (GREEN, PANEL_RAISED, "accents on a raised panel"),
    (GREEN_ON_DARK_TEXT, GREEN, "words on a primary (ember) button"),
    (ACCENT, BG, "information headings on the window"),
    (ACCENT, PANEL, "information headings on a panel"),
    (ACCENT, PANEL_RAISED, "information headings on a raised panel"),
    (WHITE, RED, "words on a destructive-action button"),
    (RED_TEXT, BG, "problem headings on the window"),
    (RED_TEXT, PANEL, "problem headings on a panel"),
]


def _linear(channel: float) -> float:
    return channel / 12.92 if channel <= 0.03928 else ((channel + 0.055) / 1.055) ** 2.4


def _luminance(hex_color: str) -> float:
    text = hex_color.lstrip("#")
    red, green, blue = (int(text[i : i + 2], 16) / 255.0 for i in (0, 2, 4))
    return 0.2126 * _linear(red) + 0.7152 * _linear(green) + 0.0722 * _linear(blue)


def contrast_ratio(foreground: str, background: str) -> float:
    """The WCAG contrast ratio between two ``#rrggbb`` colours."""
    first, second = _luminance(foreground), _luminance(background)
    lighter, darker = max(first, second), min(first, second)
    return (lighter + 0.05) / (darker + 0.05)


STYLESHEET = f"""
* {{
    font-family: "Segoe UI", "Noto Sans", sans-serif;
    font-size: 13px;
    color: {TEXT};
}}
QMainWindow, QWidget#root {{
    background: {BG};
}}
QWidget {{
    background: transparent;
}}
QFrame#panel, QFrame#banner, QFrame#messagePanel {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 8px;
}}
QFrame#banner {{
    background: {PANEL_RAISED};
}}
QLabel#appTitle {{
    font-size: 20px; font-weight: 700; color: {GREEN};
}}
QLabel#pageTitle {{
    font-size: 17px; font-weight: 700; color: {TEXT};
}}
QLabel#stateWord {{
    font-size: 22px; font-weight: 800; color: {GREEN};
}}
QLabel#sectionTitle {{
    font-size: 14px; font-weight: 700; color: {GREEN};
}}
QLabel#faint, QLabel#hint {{
    color: {TEXT_FAINT};
}}
QLabel#problemTitle {{
    font-size: 14px; font-weight: 700; color: {RED_TEXT};
}}
QLabel#infoTitle {{
    font-size: 14px; font-weight: 700; color: {ACCENT};
}}
QPushButton {{
    background: {PANEL_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 7px 14px;
    color: {TEXT};
}}
QPushButton:hover {{
    border-color: {GREEN};
}}
QPushButton:disabled {{
    color: {TEXT_DISABLED};
    border-color: {BORDER};
}}
QPushButton#primary {{
    background: {GREEN};
    color: {GREEN_ON_DARK_TEXT};
    font-weight: 700;
    border-color: {GREEN};
}}
QPushButton#primary:disabled {{
    background: {PANEL_RAISED};
    color: {TEXT_DISABLED};
    border-color: {BORDER};
}}
QPushButton#danger {{
    background: {RED};
    color: {WHITE};
    font-weight: 700;
    border-color: {RED};
}}
QPushButton#danger:disabled {{
    background: {PANEL_RAISED};
    color: {TEXT_DISABLED};
    border-color: {BORDER};
}}
QLineEdit, QComboBox, QSpinBox, QTimeEdit {{
    background: {PANEL_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    padding: 6px 8px;
    color: {TEXT};
    selection-background-color: {GREEN_DARK};
    selection-color: {TEXT};
}}
QLineEdit:disabled, QComboBox:disabled, QSpinBox:disabled {{
    color: {TEXT_DISABLED};
}}
QListWidget, QTreeWidget, QTableWidget, QTextEdit, QPlainTextEdit {{
    background: {PANEL};
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: {TEXT};
    selection-background-color: {GREEN_DARK};
    selection-color: {TEXT};
}}
QListWidget#sidebar {{
    background: {PANEL};
    font-size: 14px;
}}
QListWidget#sidebar::item {{
    padding: 10px 12px;
    border-radius: 6px;
    color: {TEXT};
}}
QListWidget#sidebar::item:selected {{
    background: {GREEN_DARK};
    color: {TEXT};
}}
QHeaderView::section {{
    background: {PANEL_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 5px;
}}
QProgressBar {{
    background: {PANEL_RAISED};
    border: 1px solid {BORDER};
    border-radius: 6px;
    color: {TEXT};
    text-align: center;
    min-height: 18px;
}}
QProgressBar::chunk {{
    background: {GREEN};
    border-radius: 5px;
}}
QCheckBox, QRadioButton {{
    color: {TEXT};
    spacing: 7px;
}}
QCheckBox:disabled, QRadioButton:disabled {{
    color: {TEXT_DISABLED};
}}
QTreeWidget::item, QListWidget::item, QTableWidget::item {{
    padding: 3px;
}}
*:focus {{
    outline: 2px solid {GREEN};
}}
QPushButton:focus, QListWidget:focus, QTreeWidget:focus,
QTableWidget:focus, QLineEdit:focus, QComboBox:focus {{
    border: 2px solid {GREEN};
}}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: {PANEL};
    border: none;
    width: 12px; height: 12px;
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {GREEN_DARK};
    border-radius: 6px;
    min-height: 24px; min-width: 24px;
}}
QToolTip {{
    background: {PANEL_RAISED};
    color: {TEXT};
    border: 1px solid {BORDER};
    padding: 4px;
}}
"""
