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


def test_imul_is_unsigned_low_word() -> None:
    # JS Math.imul low 32 bits, kept unsigned. (2^31-1)*2 -> 0xFFFFFFFE.
    assert splash._imul(2147483647, 2) == 0xFFFFFFFE
    assert splash._imul(0xFFFFFFFF, 0xFFFFFFFF) == 1


def test_hash_zero_and_determinism() -> None:
    assert splash._hash(0, 0) == 0.0
    assert splash._hash(3, 7) == splash._hash(3, 7)
    assert 0.0 <= splash._hash(3, 7) < 1.0
    assert splash._hash(1, 0) != splash._hash(0, 1)


def test_px_bounds_and_none() -> None:
    g = splash._make_grid(5)
    splash._px(g, -1, 0, (1, 1, 1))
    splash._px(g, 5, 0, (1, 1, 1))
    splash._px(g, 99, 0, (1, 1, 1))
    assert all(cell is None for row in g for cell in row)
    splash._px(g, 2, 0, (1, 1, 1))
    assert g[0][2] == (1, 1, 1)
    splash._px(g, 2, 0, None)            # None never clears
    assert g[0][2] == (1, 1, 1)


def test_carve_clears_a_filled_disc() -> None:
    g = splash._make_grid(20)
    splash._discE(g, 10, 14, 4, 4, (9, 9, 9))
    assert g[14][10] == (9, 9, 9)
    splash._carve(g, 10, 14, 4, 4)
    assert g[14][10] is None


def test_stars_are_deterministic_and_placed() -> None:
    splash._use_theme("dark")
    g1 = splash._make_grid(60)
    g2 = splash._make_grid(60)
    splash._stars(g1, 28, 0.3)
    splash._stars(g2, 28, 0.3)
    assert g1 == g2
    assert any(cell is not None for row in g1 for cell in row)


def test_imp_sprite_matches_locked_bitmap() -> None:
    splash._use_theme("dark")
    p = splash._build_palette("dark")
    g = splash._make_grid(40)
    splash._impAt(g, 16, 0)              # xc=16 -> ox=round(16-8)=8
    assert g[0][8 + 3] == p["impH"]      # IMP[0] = "   O        O   "
    assert g[5][8 + 4] == p["impO"]      # IMP[5] = "    oooooooo    "
    assert g[7][8 + 5] == p["impW"]      # IMP[7] = "  oooWbooWbooo  " glint
    assert g[7][8 + 6] == p["impB"]      # eye
    assert g[0][8 + 0] is None           # leading space = transparent


def test_imp_sprite_is_deterministic() -> None:
    splash._use_theme("dark")
    g1 = splash._make_grid(40)
    g2 = splash._make_grid(40)
    splash._impAt(g1, 16, 0)
    splash._impAt(g2, 16, 0)
    assert g1 == g2


def test_moonG_fills_a_disc() -> None:
    splash._use_theme("dark")
    p = splash._build_palette("dark")
    g = splash._make_grid(40)
    splash._moonG(g, 20, 8, 3, False)
    assert g[8][20] == p["moon"]


def test_twenty_scenes_numbered_and_phrased() -> None:
    assert len(splash.SCENES) == 20
    assert [s[0] for s in splash.SCENES] == list(range(1, 21))
    expected_phrases = [
        "a familiar, summoned to help you ship.",
        "the familiar awakens.",
        "two horns, perched and ready.",
        "whisper your wish.",
        "stir the cauldron — what's brewing?",
        "the portal is open. step through.",
        "by emberlight, we code.",
        "drawn in the stars.",
        "let's make some mischief.",
        "small imp, big leverage.",
        "conjured from the void.",
        "gaze in — what shall we build?",
        "ready to raise some hell in the diff.",
        "every great hack needs a familiar.",
        "mischief, compiled.",
        "what are we building tonight?",
        "make a wish.",
        "point me at the problem.",
        "welcome back.",
        "your imp is listening.",
    ]
    assert [s[1] for s in splash.SCENES] == expected_phrases


def test_every_builder_draws_pixels() -> None:
    splash._use_theme("dark")
    for _num, _phrase, builder in splash.SCENES:
        g = splash._make_grid(100)
        builder(g, 100)
        assert any(cell is not None for row in g for cell in row)


def test_scene_geometry_is_deterministic_at_a_width() -> None:
    splash._use_theme("dark")
    g1 = splash._make_grid(100)
    g2 = splash._make_grid(100)
    splash.SCENES[4][2](g1, 100)         # cauldron uses hash-placed bubbles
    splash.SCENES[4][2](g2, 100)
    assert g1 == g2


def test_changing_width_morphs_the_scene() -> None:
    splash._use_theme("dark")
    g_a = splash._make_grid(100)
    g_b = splash._make_grid(160)
    splash.SCENES[1][2](g_a, 100)
    splash.SCENES[1][2](g_b, 160)
    # Same scene, different width => different grids (width-responsive, not a
    # fixed block). cx/mb shift with W, so the imp/moon move.
    assert g_a != [row[:100] for row in g_b]


def test_star_lattice_is_stable_in_overlap_across_widths() -> None:
    # AC4's other half: width-independent geometry (the hash-keyed star lattice,
    # keyed on (gx, gy) grid coords, not on W) is byte-identical in the shared
    # left region across nearby widths.
    splash._use_theme("dark")
    g_a = splash._make_grid(100)
    g_b = splash._make_grid(160)
    splash._stars(g_a, splash.H, 0.30)
    splash._stars(g_b, splash.H, 0.30)
    assert [row[:100] for row in g_b] == g_a
    assert any(cell is not None for row in g_a for cell in row)
