from __future__ import annotations

import pytest

from imp.cli import palette as pal
from imp.cli import splash


@pytest.fixture(autouse=True)
def _reset_theme():
    pal.set_theme("ansi")
    yield
    pal.set_theme("ansi")


def test_anchor_roles_sourced_from_palette_dark(monkeypatch: pytest.MonkeyPatch) -> None:
    sentinel = (0x01, 0x02, 0x03)
    fake_rgb = {
        "yellow": sentinel, "magenta": sentinel, "cyan": sentinel,
        "blue": sentinel, "gray": sentinel, "bright_blue": sentinel,
    }
    monkeypatch.setattr(splash.pal, "theme_rgb", lambda _theme: fake_rgb)
    p = splash._build_palette("dark")
    assert p["impO"] == sentinel
    assert p["wm"] == sentinel
    assert p["portal"] == sentinel
    assert p["rune"] == sentinel
    assert p["star"] == sentinel
    assert p["starD"] == sentinel
    assert p["bright"] == sentinel


def test_derived_shades_match_canonical_hexes() -> None:
    p = splash._build_palette("dark")
    assert p["fire1"] == (0xB8, 0x6A, 0x1F)
    assert p["moon"] == (0xB8, 0xC2, 0xD6)
    assert p["moonGlow"] == (0x39, 0x43, 0x5C)
    assert p["moonRing"] is None          # dark crescent uses a glow, no ring
    light = splash._build_palette("light")
    assert light["moonRing"] == (0x6A, 0x72, 0x90)
    assert light["moonGlow"] is None


def test_ansi_uses_named_sgr_tokens() -> None:
    p = splash._build_palette("ansi")
    assert p["impO"] == "bright_yellow"
    assert p["star"] == "white"
    assert p["rune"] == "bright_cyan"
    assert p["moonRing"] is None


def test_fg_bg_openers_truecolor_and_named() -> None:
    assert splash._fg_open((1, 2, 3)) == "\x1b[38;2;1;2;3m"
    assert splash._bg_open((1, 2, 3)) == "\x1b[48;2;1;2;3m"
    assert splash._fg_open("bright_yellow") == "\x1b[93m"
    assert splash._bg_open("red") == "\x1b[41m"
    assert splash._fg_open(None) == ""
