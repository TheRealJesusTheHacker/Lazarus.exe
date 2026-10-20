"""The Lazarus look: green on black, and nothing purple, ever.

Every colour the window uses lives here, by name, so the contrast
of every text/background pairing the window actually uses can be
checked in one place (``PAIRINGS`` below, pinned by
``tests/test_ui_theme.py`` at 4.5:1 or better — the WCAG AA floor
for ordinary text). Red appears only as the accent on the two
destructive final actions (running a decryptor, applying a tidy-up)
and as the words of a problem heading; it is never a background for
body text and never decoration.
"""

from __future__ import annotations

# -- the palette -------------------------------------------------------

BG = "#060a07"  # the window itself: near-black, green-tinted
PANEL = "#0c130d"  # panels and cards sitting on the window
PANEL_RAISED = "#111a12"  # a panel lifted one step (inputs, banners)
TEXT = "#e8f2ea"  # the words people read
TEXT_FAINT = "#7d927f"  # quieter words: still 5.3:1 or better everywhere used
TEXT_DISABLED = "#9db3a0"  # disabled words — lighter than faint on purpose:
# a disabled control must stay readable, so its colour is chosen to
# clear 4.5:1 on every panel colour, not to look washed out.
GREEN = "#2ee06f"  # the brand green: headings, accents, primary buttons
GREEN_DARK = "#14532d"  # selection background behind TEXT
GREEN_ON_DARK_TEXT = "#06130a"  # the words on a GREEN button
BORDER = "#223526"  # hairlines between panels (not text; no ratio owed)
WHITE = "#ffffff"  # the words on the red accent
RED = "#a31f1f"  # accent background for the two destructive final actions
RED_TEXT = "#ff8a7a"  # problem headings, on BG/PANEL only

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
    (GREEN_ON_DARK_TEXT, GREEN, "words on a primary (green) button"),
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
    font-size: 14px; font-weight: 700; color: {GREEN};
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
