"""The Lazarus look, pinned: contrast, and no purple anywhere."""

from __future__ import annotations

import re

from lazarus.ui import theme


def test_contrast_ratio_sanity():
    assert theme.contrast_ratio("#000000", "#ffffff") > 20.9
    assert theme.contrast_ratio("#ffffff", "#000000") > 20.9
    assert theme.contrast_ratio("#2ee06f", "#2ee06f") == 1.0
    assert 4.0 < theme.contrast_ratio("#767676", "#ffffff") < 5.0


def test_every_pairing_the_window_uses_clears_aa():
    assert theme.PAIRINGS, "the pairing list must not be empty"
    for foreground, background, where in theme.PAIRINGS:
        ratio = theme.contrast_ratio(foreground, background)
        assert ratio >= 4.5, f"{where}: {foreground} on {background} is {ratio:.2f}:1"


def _palette_hexes():
    values = []
    for name in dir(theme):
        if name.isupper():
            value = getattr(theme, name)
            if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{6}", value):
                values.append((name, value))
    return values


def test_no_purple_anywhere_in_the_palette():
    # Purple means blue strongest with red close behind and green
    # suppressed. Nothing in the palette may sit in that country.
    for name, value in _palette_hexes():
        red = int(value[1:3], 16)
        green = int(value[3:5], 16)
        blue = int(value[5:7], 16)
        assert not (blue > red and blue > green), f"{name}={value} reads purple"
        assert not (blue > green and red > green and blue - green > 40), (
            f"{name}={value} reads purple"
        )


def test_no_purple_hex_hiding_in_the_stylesheet():
    for value in re.findall(r"#[0-9a-fA-F]{6}", theme.STYLESHEET):
        red = int(value[1:3], 16)
        green = int(value[3:5], 16)
        blue = int(value[5:7], 16)
        assert not (blue > red and blue > green), f"{value} in stylesheet reads purple"


def test_the_five_brand_colours_are_the_ones_specified():
    assert theme.BG == "#060a07"
    assert theme.PANEL == "#0c130d"
    assert theme.TEXT == "#e8f2ea"
    assert theme.TEXT_FAINT == "#7d927f"
    assert theme.GREEN == "#2ee06f"
    assert theme.GREEN_DARK == "#14532d"


def test_disabled_text_is_its_own_readable_colour():
    assert theme.TEXT_DISABLED != theme.TEXT_FAINT
    for background in (theme.BG, theme.PANEL, theme.PANEL_RAISED):
        assert theme.contrast_ratio(theme.TEXT_DISABLED, background) >= 4.5
