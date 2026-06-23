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
        imp = any(g[y][x] == splash._P["impBody"]
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
        assert any(g[y][x] == splash._P["impBody"]
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
        assert any(g[y][x] == splash._P["impBody"]
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
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_planet_paints_both_a_body_disc_and_a_ring() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._planet(g, 24, 7, 4)
    body = any(cell == splash._P["planet"] for row in g for cell in row)
    ring = any(cell == splash._P["planetRing"] for row in g for cell in row)
    assert body and ring


def test_task4_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_aurora_scene", "the sky's showing off — let's match it."),
        ("_fireflies", "the lights are out to play."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_aurora_paints_both_band_colors_only_in_the_accent_columns() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._aurora(g)
    painted = [(x, cell) for row in g for x, cell in enumerate(row) if cell is not None]
    assert any(cell == splash._P["aurora1"] for _, cell in painted)
    assert any(cell == splash._P["aurora2"] for _, cell in painted)
    assert all(x >= 18 for x, _ in painted)


def test_task5_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_will_o_wisp", "follow the light."),
        ("_floating_wisps", "the void is listening."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_wisp_paints_a_bright_core_inside_a_dim_glow() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._wisp(g, 25, 7, 4)
    core = any(cell == splash._P["wisp"] for row in g for cell in row)
    glow = any(cell == splash._P["wispGlow"] for row in g for cell in row)
    assert core and glow

    g2 = splash._make_grid()
    splash._wisp(g2, 25, 7, 2)
    core2 = any(cell == splash._P["wisp"] for row in g2 for cell in row)
    glow2 = any(cell == splash._P["wispGlow"] for row in g2 for cell in row)
    assert core2 and glow2


def test_task6_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_twin_candles", "whisper your wish."),
        ("_twin_torches", "let's raise a little hell."),
        ("_glowing_mushrooms", "let's make some mischief."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_mushroom_paints_a_glowing_cap_above_its_stem() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._mushroom(g, 27, 12, 3)
    cap = [(x, y) for y, row in enumerate(g)
           for x, cell in enumerate(row) if cell == splash._P["mushroomCap"]]
    stem = [(x, y) for y, row in enumerate(g)
            for x, cell in enumerate(row) if cell == splash._P["mushroomStem"]]
    assert cap and stem
    assert min(y for _, y in cap) < max(y for _, y in stem)


def test_task1p3_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_summoning_circle", "the circle is drawn."),
        ("_open_portal", "the portal is open. step through."),
        ("_rune_sigil", "the runes are ready."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_portal_gate_paints_a_glowing_ring_around_a_dark_core() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._portal_gate(g, 25, 7)
    rim = any(cell == splash._P["portal"] for row in g for cell in row)
    core = any(cell == splash._P["portalCore"] for row in g for cell in row)
    assert rim and core


def test_summon_ring_paints_a_sigil_ring_and_accents() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._summon_ring(g, 24, 11)
    ring = any(cell == splash._P["sigilDim"] for row in g for cell in row)
    accent = any(cell == splash._P["sigil"] for row in g for cell in row)
    assert ring and accent


def test_task2p3_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_crystal_ball_scene", "gaze in — what shall we build?"),
        ("_lantern_scene", "the lantern's lit. lead the way."),
        ("_crystal_cluster", "small imp, big leverage."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_orb_paints_a_glass_body_with_a_bright_glint() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._orb(g, 24, 7, 3)
    body = any(cell == splash._P["glass"] for row in g for cell in row)
    glint = any(cell == splash._P["glassCore"] for row in g for cell in row)
    assert body and glint


def test_lantern_paints_a_frame_around_a_flame() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._lantern(g, 24, 4)
    frame = any(cell == splash._P["lanternFrame"] for row in g for cell in row)
    flame = any(cell == splash._P["fireTip"] for row in g for cell in row)
    assert frame and flame


def test_crystal_paints_a_lit_tip_above_its_body() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._crystal(g, 25, 12, 6)
    body_ys = [y for y, row in enumerate(g)
               for cell in row if cell == splash._P["crystal"]]
    tip_ys = [y for y, row in enumerate(g)
              for cell in row if cell == splash._P["crystalLight"]]
    assert body_ys and tip_ys
    assert min(tip_ys) < min(body_ys)


def test_task3p3_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_cauldron_scene", "stir the cauldron — what's brewing?"),
        ("_grimoire_scene", "the grimoire is open."),
        ("_potion_shelf_scene", "every spell has a price — name yours."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_cauldron_paints_brew_bubbles_above_the_iron_body() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._cauldron(g, 24, 11)
    iron = [(x, y) for y, row in enumerate(g)
            for x, c in enumerate(row) if c == splash._P["iron"]]
    brew = [(x, y) for y, row in enumerate(g)
            for x, c in enumerate(row) if c == splash._P["brew"]]
    assert iron and brew
    assert min(y for _, y in brew) < min(y for _, y in iron)


def test_task4p3_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_hourglass_scene", "time's a construct. ship anyway."),
        ("_scroll_scene", "the ink is wet. dictate."),
        ("_quill_scene", "name your desire."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_hourglass_paints_sand_in_both_chambers() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    top_y = 3
    splash._hourglass(g, 24, top_y)
    sand_y = [y for y, row in enumerate(g)
              for c in row if c == splash._P["sand"]]
    assert any(y <= top_y + 1 for y in sand_y)
    assert any(y >= top_y + 6 for y in sand_y)


def test_task5p3_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_raven_scene", "tell me what haunts your codebase."),
        ("_black_cat_scene", "back so soon? good."),
        ("_bats_scene", "creatures of the night, assemble."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_raven_paints_a_silhouette_with_a_glowing_eye() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._raven(g, 23, 11)
    body = any(c == splash._P["creature"] for row in g for c in row)
    eye = any(c == splash._P["creatureEye"] for row in g for c in row)
    assert body and eye


def test_black_cat_paints_a_silhouette_with_glowing_eyes() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._black_cat(g, 23, 12)
    body = any(c == splash._P["creature"] for row in g for c in row)
    eyes = any(c == splash._P["creatureEye"] for row in g for c in row)
    assert body and eyes


def test_task6p3_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_jack_o_lantern_scene", "trouble? my specialty."),
        ("_skeleton_key_scene", "i know a shortcut — only slightly cursed."),
        ("_top_hat_scene", "now you see it. now you ship it."),
    ]
    for name, tagline in cases:
        builder = getattr(splash, name)
        assert (builder, tagline) in splash.SCENES, name
        splash._use_theme("dark")
        g = splash._make_grid()
        builder(g)
        assert any(g[y][x] is not None
                   for y in range(splash.H) for x in range(18, splash.W)), name
        assert any(g[y][x] == splash._P["impBody"]
                   for y in range(splash.H) for x in range(2, 18)), name


def test_pumpkin_paints_a_carved_face_in_its_body() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._pumpkin(g, 24, 9)
    body = any(c == splash._P["pumpkin"] for row in g for c in row)
    face = any(c == splash._P["impEye"] for row in g for c in row)
    assert body and face


def test_grimoire_paints_open_pages_with_a_cover() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._grimoire(g, 24, 8)
    assert any(c == splash._P["bookPage"] for row in g for c in row)
    assert any(c == splash._P["bookCover"] for row in g for c in row)


def test_potion_shelf_paints_potions_on_an_iron_shelf() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._potion_shelf(g, 11)
    assert any(c == splash._P["iron"] for row in g for c in row)
    assert any(c == splash._P["brew"] for row in g for c in row)


def test_scroll_paints_pages_with_ink_lines() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._scroll(g, 24, 7)
    assert any(c == splash._P["bookPage"] for row in g for c in row)
    assert any(c == splash._P["ink"] for row in g for c in row)


def test_quill_ink_paints_a_quill_over_an_inkwell() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._quill_ink(g, 22, 11)
    assert any(c == splash._P["quill"] for row in g for c in row)
    assert any(c == splash._P["ink"] for row in g for c in row)


def test_bat_paints_a_creature_silhouette() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._bat(g, 24, 7)
    assert any(c == splash._P["creature"] for row in g for c in row)


def test_skeleton_key_paints_a_gold_ring_and_shaft() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._skeleton_key(g, 21, 7)
    assert any(c == splash._P["gold"] for row in g for c in row)


def test_top_hat_paints_a_dark_hat_with_a_band() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._top_hat(g, 24, 12)
    assert any(c == splash._P["creature"] for row in g for c in row)
    assert any(c == splash._P["bookCover"] for row in g for c in row)


def test_task1p4_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_wand_scene", "speak, and it shall be built."),
        ("_tarot_scene", "pick a card. any card."),
        ("_fairy_ring", "step inside the ring."),
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


def test_tarot_raises_the_center_card_and_marks_it_with_a_sigil() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._tarot(g, 25, 12)
    assert g[9][25] == splash._P["sigil"]
    faces = any(c == splash._P["bookPage"] for row in g for c in row)
    borders = any(c == splash._P["bookCover"] for row in g for c in row)
    assert faces and borders


def test_task2p4_scenes_are_wired_and_paint_imp_and_accent() -> None:
    cases = [
        ("_spider_web_scene", "caught in a bug? i'll cut it loose."),
        ("_ouroboros_scene", "round and round the refactor goes."),
        ("_bug_scene", "show me the bug. i'll deal with it."),
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


def test_bug_paints_a_red_shell_over_a_dark_body_with_a_glowing_eye() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._bug(g, 24, 9)
    shell = any(c == splash._P["bookCover"] for row in g for c in row)
    body = any(c == splash._P["creature"] for row in g for c in row)
    eye = any(c == splash._P["creatureEye"] for row in g for c in row)
    assert shell and body and eye
