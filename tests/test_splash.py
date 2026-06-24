from __future__ import annotations

from imp.cli import splash


def test_dark_table_copies_gallery_hexes_verbatim() -> None:
    d = splash._build_palette("dark")
    assert d["o"] == (0xFF, 0x9A, 0x3C)
    assert d["b"] == (0x2A, 0x16, 0x06)
    assert d["moon"] == (0xB8, 0xC2, 0xD6)
    assert d["star"] == (0x7F, 0xA2, 0xCA)
    assert d["tag"] == (0x9A, 0xA0, 0xA8)
    assert d["voidCore"] == (0x06, 0x04, 0x0A)


def test_ansi_table_uses_only_named_tokens() -> None:
    a = splash._build_palette("ansi")
    assert all(v is None or v in splash._NAMED for v in a.values())


def test_light_table_defines_every_dark_role() -> None:
    assert set(splash._build_palette("light")) == set(splash._build_palette("dark"))
    assert set(splash._build_palette("ansi")) == set(splash._build_palette("dark"))


def test_unknown_theme_falls_back_to_ansi() -> None:
    assert splash._build_palette("nope") == splash._build_palette("ansi")


def test_use_theme_sets_the_module_palette() -> None:
    splash._use_theme("dark")
    assert splash._P is splash._DARK


def test_fg_open_emits_truecolor_for_tuple_and_named_code_for_str() -> None:
    assert splash._fg_open((255, 154, 60)) == "\x1b[38;2;255;154;60m"
    assert splash._fg_open("red") == "\x1b[31m"
    assert splash._fg_open(None) == ""


def test_bg_open_emits_truecolor_and_named_background_codes() -> None:
    assert splash._bg_open((0, 255, 0)) == "\x1b[48;2;0;255;0m"
    assert splash._bg_open("red") == "\x1b[41m"
    assert splash._bg_open(None) == ""


def test_imp_sprite_is_sixteen_wide_and_fifteen_tall() -> None:
    assert len(splash.IMP) == 15
    assert all(len(row) == 16 for row in splash.IMP)
