from __future__ import annotations

from imp.cli import splash


def test_dark_palette_locks_the_spec_imp_hexes() -> None:
    dark = splash._build_palette("dark")
    assert dark["impBody"] == (0xFF, 0x9A, 0x3C)
    assert dark["impHorn"] == (0xC2, 0x61, 0x1F)
    assert dark["impGlint"] == (0xFF, 0xE9, 0xC8)
    assert dark["impEye"] == (0x2A, 0x16, 0x06)
    assert dark["tag"] == (0x9A, 0xA0, 0xA8)


def test_use_theme_sets_the_module_palette() -> None:
    splash._use_theme("dark")
    assert splash._P is splash._DARK


def test_ansi_palette_uses_only_named_tokens_never_rgb_tuples() -> None:
    ansi = splash._build_palette("ansi")
    assert all(v is None or v in splash._NAMED for v in ansi.values())


def test_unknown_theme_falls_back_to_ansi() -> None:
    assert splash._build_palette("nope") == splash._build_palette("ansi")


def test_fg_open_emits_truecolor_for_tuple_and_named_code_for_str() -> None:
    assert splash._fg_open((255, 154, 60)) == "\x1b[38;2;255;154;60m"
    assert splash._fg_open("red") == "\x1b[31m"
    assert splash._fg_open(None) == ""


def test_bg_open_emits_truecolor_and_named_background_codes() -> None:
    assert splash._bg_open((0, 255, 0)) == "\x1b[48;2;0;255;0m"
    assert splash._bg_open("red") == "\x1b[41m"
    assert splash._bg_open(None) == ""
