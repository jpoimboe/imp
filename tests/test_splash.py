from __future__ import annotations

import importlib

import pytest

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


def test_px_out_of_bounds_is_a_silent_no_op() -> None:
    g = splash._make_grid()
    splash._px(g, -1, 0, (1, 1, 1))
    splash._px(g, splash.W + 5, 0, (1, 1, 1))
    splash._px(g, 0, splash.H + 5, (1, 1, 1))
    assert all(cell is None for row in g for cell in row)


def test_px_in_bounds_sets_the_cell() -> None:
    g = splash._make_grid()
    splash._px(g, 5, 6, (1, 2, 3))
    assert g[6][5] == (1, 2, 3)


def test_make_grid_is_exactly_thirty_by_fifteen() -> None:
    g = splash._make_grid()
    assert len(g) == splash.H == 15
    assert all(len(row) == splash.W == 30 for row in g)


def test_hash_is_deterministic_in_range_and_varies_by_input() -> None:
    assert splash._hash(3, 7) == splash._hash(3, 7)
    assert 0.0 <= splash._hash(3, 7) < 1.0
    assert splash._hash(3, 7) != splash._hash(4, 7)


def test_stars_are_confined_to_the_accent_columns() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._stars(g, 0, splash.H, 0.95)
    placed = [(x, y) for y, row in enumerate(g)
              for x, cell in enumerate(row) if cell is not None]
    assert placed, "expected the dense starfield to place at least one star"
    assert all(x >= 19 for x, _ in placed)


def test_ground_fills_a_row_and_leaves_a_glow_above() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._ground(g, 13, glow=True)
    assert all(g[13][x] == splash._P["ground"] for x in range(splash.W))
    assert any(g[12][x] == splash._P["groundGlow"] for x in range(splash.W))


def test_imp_sprite_is_sixteen_wide_and_fifteen_tall() -> None:
    assert len(splash.IMP) == 15
    assert all(len(row) == 16 for row in splash.IMP)


def test_left_imp_anchors_at_column_two_with_themed_colors() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._left_imp(g)
    # Horn 'O' at sprite (row 0, col 3) -> grid column 2 + 3 = 5.
    assert g[0][5] == splash._P["impHorn"]
    # Eye glint 'W' at sprite (row 7, col 5) -> grid column 2 + 5 = 7.
    assert g[7][7] == splash._P["impGlint"]
    # Eye 'b' at sprite (row 7, col 6) -> grid column 2 + 6 = 8.
    assert g[7][8] == splash._P["impEye"]
    # Body 'o' present somewhere in the imp's column band (2..17).
    assert any(g[5][x] == splash._P["impBody"] for x in range(2, 18))


def test_left_imp_never_paints_the_accent_columns() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._left_imp(g)
    assert all(g[y][x] is None for y in range(splash.H) for x in range(18, splash.W))


def test_cell_is_a_space_when_both_halves_are_transparent() -> None:
    assert splash._cell(None, None, color=True) == " "
    assert splash._cell(None, None, color=False) == " "


def test_cell_emits_top_fg_bottom_bg_upper_half_block_when_colored() -> None:
    out = splash._cell((255, 0, 0), (0, 255, 0), color=True)
    assert out == "\x1b[38;2;255;0;0m\x1b[48;2;0;255;0m▀\x1b[0m"


def test_cell_color_off_uses_a_bare_half_block_with_no_escapes() -> None:
    assert splash._cell((255, 0, 0), None, color=False) == "▀"
    assert splash._cell(None, (0, 255, 0), color=False) == "▄"
    assert "\x1b[" not in splash._cell((1, 2, 3), (4, 5, 6), color=False)


def test_encode_row_pairs_top_and_bottom_and_rstrips_trailing_space() -> None:
    g = splash._make_grid()
    g[0][0] = (255, 0, 0)
    g[1][0] = (0, 255, 0)
    row = splash._encode_row(g, 0, 1, color=True)
    assert row == "\x1b[38;2;255;0;0m\x1b[48;2;0;255;0m▀\x1b[0m"


def test_encode_grid_yields_eight_lines_and_handles_the_odd_last_row() -> None:
    g = splash._make_grid()
    g[14][19] = (1, 2, 3)  # last row has no paired bottom row (index 15 is absent)
    lines = splash._encode_grid(g, color=False)
    assert len(lines) == 8
    assert lines[7].endswith("▀")


@pytest.mark.parametrize("theme", ["dark", "light", "ansi"])
def test_every_scene_paints_both_the_imp_and_an_accent_in_bounds(theme) -> None:
    splash._use_theme(theme)
    for builder, _tagline in splash.SCENES:
        g = splash._make_grid()
        builder(g)  # must not raise (all drawing is bounds-clamped)
        assert len(g) == splash.H and all(len(r) == splash.W for r in g)
        imp = any(g[y][x] is not None
                  for y in range(splash.H) for x in range(2, 18))
        accent = any(g[y][x] is not None
                     for y in range(splash.H) for x in range(18, splash.W))
        assert imp, f"{builder.__name__} drew no imp in {theme}"
        assert accent, f"{builder.__name__} drew no accent in {theme}"


def test_scenes_pairs_carry_str_or_callable_taglines() -> None:
    assert splash.SCENES, "the pool must be non-empty"
    for _builder, tagline in splash.SCENES:
        assert isinstance(tagline, str) or callable(tagline)


@pytest.mark.parametrize("hour,word", [
    (20, "tonight"), (23, "tonight"), (4, "tonight"), (18, "tonight"),
    (5, "today"), (12, "today"), (17, "today"),
])
def test_time_of_day_tagline_resolves_today_vs_tonight(monkeypatch, hour, word) -> None:
    monkeypatch.setattr(splash, "_now_hour", lambda: hour)
    assert splash._tonight_or_today() == f"what are we building {word}?"


def test_choose_index_stays_within_the_pool() -> None:
    for _ in range(200):
        assert 0 <= splash.choose_index() < len(splash.SCENES)


def test_scene_lines_renders_the_same_chosen_scene_identically() -> None:
    a = splash.scene_lines(0, theme="dark", color=True)
    b = splash.scene_lines(0, theme="dark", color=True)
    assert a == b


def test_ansi_render_emits_only_named_codes_never_truecolor() -> None:
    out = "\n".join(splash.scene_lines(4, theme="ansi", color=True))
    assert "38;2" not in out
    assert "48;2" not in out


def test_splash_block_is_a_nonempty_multiline_scene_per_theme() -> None:
    for theme in ("dark", "light", "ansi"):
        out = splash.splash_block(theme=theme, width=40, color=True)
        assert out
        assert "\n" in out
        assert "▀" in out


def test_splash_block_no_color_returns_plain_tagline_only(monkeypatch) -> None:
    monkeypatch.setattr(splash, "choose_index", lambda: 0)
    out = splash.splash_block(theme="dark", width=40, color=False)
    assert out == "the familiar awakens."
    assert "▀" not in out
    assert "\x1b[" not in out


def test_splash_block_narrow_width_returns_plain_tagline_only(monkeypatch) -> None:
    monkeypatch.setattr(splash, "choose_index", lambda: 2)
    out = splash.splash_block(theme="dark", width=31, color=True)
    assert out == "make a wish."
    assert "▀" not in out
    assert "\x1b[" not in out


def test_splash_block_tagline_is_grey_blue_italic_when_rendered(monkeypatch) -> None:
    monkeypatch.setattr(splash, "choose_index", lambda: 0)
    out = splash.splash_block(theme="dark", width=40, color=True)
    assert out.endswith("\x1b[38;2;154;160;168m\x1b[3mthe familiar awakens.\x1b[0m")


def test_main_wires_the_splash_module() -> None:
    main = importlib.import_module("imp.cli.main")
    assert main.splash.splash_block is splash.splash_block


def test_retired_opening_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("imp.cli.opening")


def test_task1_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_constellation_scene", "drawn in the stars."),
        ("_comet_scene", "something wicked this way ships."),
        ("_the_void", "conjured from the void."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(2, 18)), name


def test_every_scene_renders_under_all_three_themes_without_error() -> None:
    for index in range(len(splash.SCENES)):
        for theme in ("dark", "light", "ansi"):
            lines = splash.scene_lines(index, theme=theme, color=True)
            assert len(lines) == 8


def test_task2_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_crescent_perch", "perched and ready."),
        ("_mountain_moon", "welcome back."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(2, 18)), name


def test_mountains_paint_a_snow_cap_above_the_peak_fill() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._mountains(g, 13)
    snow = [(x, y) for y, row in enumerate(g)
            for x, cell in enumerate(row) if cell == splash._P["snow"]]
    peak = [(x, y) for y, row in enumerate(g)
            for x, cell in enumerate(row) if cell == splash._P["peak"]]
    assert snow and peak
    assert min(y for _, y in snow) < min(y for _, y in peak)


def test_task3_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_ringed_planet", "a world, from nothing."),
        ("_galaxy_scene", "lost in the stack? i know the way."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(2, 18)), name


def test_planet_paints_both_a_body_disc_and_a_ring() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._planet(g, 24, 7, 4)
    body = any(cell == splash._P["planet"] for row in g for cell in row)
    ring = any(cell == splash._P["planetRing"] for row in g for cell in row)
    assert body and ring
