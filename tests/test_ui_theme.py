"""The Lazarus look, pinned: contrast, and no purple anywhere."""

from __future__ import annotations

import re

from lazarus.ui import theme


def test_contrast_ratio_sanity():
    assert theme.contrast_ratio("#000000", "#ffffff") > 20.9
    assert theme.contrast_ratio("#ffffff", "#000000") > 20.9
    assert theme.contrast_ratio("#f0a13c", "#f0a13c") == 1.0
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


def test_the_brand_colours_are_the_ones_specified():
    # The namesake palette (fleet program, 2026-10-20): warm
    # stone-black, bone, ember amber — "first light". Pinned so a
    # well-meaning pass cannot drift the face back to the shared
    # fleet green or anywhere else.
    assert theme.BG == "#0d0a07"
    assert theme.PANEL == "#171209"
    assert theme.PANEL_RAISED == "#120d08"
    assert theme.TEXT == "#f2ead9"
    assert theme.TEXT_FAINT == "#b3a58c"
    assert theme.TEXT_DISABLED == "#9a8c6f"
    assert theme.GREEN == "#f0a13c"
    assert theme.GREEN_DARK == "#4d3416"
    assert theme.GREEN_ON_DARK_TEXT == "#1a0f02"
    assert theme.ACCENT == "#d8c391"
    assert theme.BORDER == "#33291a"


def test_danger_is_the_fleet_red_in_every_tool():
    assert theme.RED == "#b03a2e"
    assert theme.WHITE == "#ffffff"
    assert theme.RED_TEXT == "#ff8a7a"


def test_spec_name_aliases_carry_the_same_colours():
    assert theme.PRIMARY == theme.GREEN
    assert theme.PRIMARY_DARK_TEXT == theme.GREEN_ON_DARK_TEXT
    assert theme.SELECTION == theme.GREEN_DARK
    assert theme.INPUT_BG == theme.PANEL_RAISED
    assert theme.PANEL_BORDER == theme.BORDER
    assert theme.DISABLED_TEXT == theme.TEXT_DISABLED


def test_disabled_text_is_its_own_readable_colour():
    assert theme.TEXT_DISABLED != theme.TEXT_FAINT
    for background in (theme.BG, theme.PANEL, theme.PANEL_RAISED):
        assert theme.contrast_ratio(theme.TEXT_DISABLED, background) >= 4.5
