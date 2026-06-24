from __future__ import annotations

import importlib
import io

import pytest

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


def test_make_grid_is_exactly_thirty_by_fifteen() -> None:
    g = splash._make_grid()
    assert len(g) == splash.H == 15
    assert all(len(row) == splash.W == 30 for row in g)


def test_hash_is_deterministic_in_range_and_varies_by_input() -> None:
    assert splash._hash(3, 7) == splash._hash(3, 7)
    assert 0.0 <= splash._hash(3, 7) < 1.0
    assert splash._hash(3, 7) != splash._hash(4, 7)
    # Pin the exact JS-gallery hash output so a change to the constants/shifts is caught.
    assert splash._hash(1, 3) == pytest.approx(0.35122208995744586)
    assert splash._hash(2, 7) == pytest.approx(0.4000562112778425)
    assert splash._hash(3, 11) == pytest.approx(0.6067249327898026)


def test_px_rounds_half_toward_positive_infinity() -> None:
    g = splash._make_grid()
    splash._px(g, 2.5, 0.0, (9, 9, 9))
    assert g[0][3] == (9, 9, 9)  # Math.round(2.5) == 3


def test_px_out_of_bounds_is_a_silent_no_op() -> None:
    g = splash._make_grid()
    splash._px(g, -1, 0, (1, 1, 1))
    splash._px(g, splash.W + 5, 0, (1, 1, 1))
    splash._px(g, 0, splash.H + 5, (1, 1, 1))
    assert all(cell is None for row in g for cell in row)


def test_line_truncates_endpoints_toward_zero_not_rounding() -> None:
    g = splash._make_grid()
    splash._line(g, 0.9, 0.0, 3.9, 0.0, (1, 1, 1))
    assert g[0][0] == (1, 1, 1)  # 0.9 truncates to 0
    assert g[0][4] is None       # 3.9 truncates to 3 (rounding would have set column 4)


def test_disc_fills_an_ellipse_and_carve_clears_it() -> None:
    g = splash._make_grid()
    splash._disc(g, 5, 5, 3, 3, (2, 2, 2))
    assert g[5][5] == (2, 2, 2)
    splash._carve(g, 5, 5, 3, 3)
    assert g[5][5] is None


def test_ring_paints_a_band_but_not_the_centre() -> None:
    g = splash._make_grid()
    splash._ring(g, 10, 7, 4, 4, (3, 3, 3), 0.6, 1.0)
    assert g[7][10] is None  # centre (d == 0) is outside the band
    assert any(c == (3, 3, 3) for row in g for c in row)


def test_stars_places_at_most_round_dens_times_34_and_confines_to_accent_columns() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._stars(g, splash.H, 1.0)  # N = round(34) = 34
    placed = [(x, y) for y, row in enumerate(g) for x, c in enumerate(row) if c is not None]
    assert 0 < len(placed) <= 34
    assert all(x >= 19 for x, _ in placed)


def test_stars_bounded_by_max_y_and_empty_at_zero_density() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._stars(g, 8, 0.5)
    assert all(y < 8 for y, row in enumerate(g) for c in row if c is not None)
    g2 = splash._make_grid()
    splash._stars(g2, splash.H, 0.0)  # N = round(0) = 0
    assert all(c is None for row in g2 for c in row)


def test_glow_ring_paints_only_odd_parity_empty_cells() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._glow_ring(g, 24, 7, 4, (5, 5, 5))
    painted = [(x, y) for y, row in enumerate(g) for x, c in enumerate(row) if c == (5, 5, 5)]
    assert painted
    assert all((x + y) & 1 for x, y in painted)


def test_ground_fills_the_row_with_the_given_colour() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._ground(g, 13, splash._P["mtnNear"])
    assert all(g[13][x] == splash._P["mtnNear"] for x in range(splash.W))


def test_left_imp_anchors_horn_glint_and_eye_at_column_two() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._L(g)
    assert g[0][5] == splash._P["O"]   # horn 'O' sprite (row0,col3) -> grid col 2+3
    assert g[7][7] == splash._P["W"]   # glint 'W' sprite (row7,col5) -> grid col 2+5
    assert g[7][8] == splash._P["b"]   # eye 'b' sprite (row7,col6) -> grid col 2+6
    assert any(g[5][x] == splash._P["o"] for x in range(2, 18))


def test_imp_offset_shifts_the_sprite_up() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._imp(g, 2, -1)
    assert g[0][5] is None            # horn row pushed off the top at oy=-1
    assert any(c == splash._P["o"] for row in g for c in row)


def test_cell_is_a_space_when_both_halves_are_transparent() -> None:
    assert splash._cell(None, None, color=True) == " "
    assert splash._cell(None, None, color=False) == " "


def test_cell_emits_top_fg_bottom_bg_upper_half_block_when_coloured() -> None:
    out = splash._cell((255, 0, 0), (0, 255, 0), color=True)
    assert out == "\x1b[38;2;255;0;0m\x1b[48;2;0;255;0m▀\x1b[0m"


def test_cell_colored_top_only_emits_fg_and_upper_block() -> None:
    assert splash._cell((255, 0, 0), None, color=True) == "\x1b[38;2;255;0;0m▀\x1b[0m"


def test_cell_colored_bottom_only_emits_bottom_fg_and_lower_block() -> None:
    assert splash._cell(None, (0, 255, 0), color=True) == "\x1b[38;2;0;255;0m▄\x1b[0m"


def test_cell_colour_off_uses_a_bare_half_block_with_no_escapes() -> None:
    assert splash._cell((255, 0, 0), None, color=False) == "▀"
    assert splash._cell(None, (0, 255, 0), color=False) == "▄"
    assert "\x1b[" not in splash._cell((1, 2, 3), (4, 5, 6), color=False)
    assert splash._cell((1, 2, 3), (4, 5, 6), color=False) == "▀"


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


@pytest.mark.parametrize("hour,period", [
    (20, "night"), (23, "night"), (4, "night"), (0, "night"),
    (5, "day"), (12, "day"), (19, "day"),
])
def test_period_splits_night_at_2000_and_day_at_0500(hour, period) -> None:
    assert splash._period(hour) == period


def test_eligible_includes_every_any_scene_in_both_periods(monkeypatch) -> None:
    any_idx = {i for i, (_b, _p, w) in enumerate(splash.SCENES) if w == splash.ANY}
    monkeypatch.setattr(splash, "_now_hour", lambda: 23)  # night
    night = set(splash._eligible())
    monkeypatch.setattr(splash, "_now_hour", lambda: 12)  # day
    day = set(splash._eligible())
    assert any_idx and any_idx <= night and any_idx <= day


def test_choose_index_only_returns_eligible_indices(monkeypatch) -> None:
    monkeypatch.setattr(splash, "_now_hour", lambda: 12)
    eligible = set(splash._eligible())
    for _ in range(200):
        assert splash.choose_index() in eligible


def test_scenes_are_builder_phrase_when_triples() -> None:
    assert splash.SCENES
    for builder, phrase, when in splash.SCENES:
        assert callable(builder)
        assert isinstance(phrase, str)
        assert when in (splash.ANY, splash.DAY, splash.NIGHT)


def test_scene_lines_is_deterministic() -> None:
    assert splash.scene_lines(0, theme="dark", color=True) == \
        splash.scene_lines(0, theme="dark", color=True)


def test_scene_lines_always_yields_eight_rows_in_every_theme() -> None:
    for index in range(len(splash.SCENES)):
        for theme in ("dark", "light", "ansi"):
            assert len(splash.scene_lines(index, theme=theme, color=True)) == 8


def test_ansi_render_emits_only_named_codes_never_truecolor() -> None:
    for index in range(len(splash.SCENES)):
        out = "\n".join(splash.scene_lines(index, theme="ansi", color=True))
        assert "38;2" not in out and "48;2" not in out


@pytest.mark.parametrize("theme", ["dark", "light", "ansi"])
def test_every_scene_paints_imp_and_accent_in_bounds(theme) -> None:
    splash._use_theme(theme)
    for builder, _phrase, _when in splash.SCENES:
        g = splash._make_grid()
        builder(g)  # must not raise (all drawing is bounds-clamped)
        assert len(g) == splash.H and all(len(r) == splash.W for r in g)
        imp = any(g[y][x] == splash._P["o"]
                  for y in range(splash.H) for x in range(2, 18))
        accent = any(g[y][x] is not None
                     for y in range(splash.H) for x in range(18, splash.W))
        assert imp, f"{builder.__name__} drew no imp in {theme}"
        assert accent, f"{builder.__name__} drew no accent in {theme}"


def test_splash_block_renders_a_multiline_scene_per_theme() -> None:
    for theme in ("dark", "light", "ansi"):
        out = splash.splash_block(theme=theme, width=40, color=True)
        assert out
        assert "\n" in out
        assert "▀" in out


def test_splash_block_no_colour_returns_the_plain_tagline_only(monkeypatch) -> None:
    monkeypatch.setattr(splash, "choose_index", lambda: 0)
    out = splash.splash_block(theme="dark", width=40, color=False)
    assert out == splash.SCENES[0][1]
    assert "▀" not in out and "\x1b[" not in out


def test_splash_block_narrow_width_returns_the_plain_tagline_only(monkeypatch) -> None:
    monkeypatch.setattr(splash, "choose_index", lambda: 0)
    out = splash.splash_block(theme="dark", width=31, color=True)
    assert out == splash.SCENES[0][1]
    assert "▀" not in out and "\x1b[" not in out


def test_splash_block_tagline_is_grey_blue_italic_when_rendered(monkeypatch) -> None:
    monkeypatch.setattr(splash, "choose_index", lambda: 0)
    out = splash.splash_block(theme="dark", width=40, color=True)
    assert out.endswith("\x1b[38;2;154;160;168m\x1b[3m" + splash.SCENES[0][1] + "\x1b[0m")


def test_main_wires_the_splash_module() -> None:
    main = importlib.import_module("imp.cli.main")
    assert main.splash.splash_block is splash.splash_block


def test_retired_opening_module_is_gone() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("imp.cli.opening")


def test_print_splash_banner_emits_blank_line_then_scene_when_interactive(monkeypatch) -> None:
    import imp.cli.main as m
    monkeypatch.setattr(m.splash, "splash_block", lambda **_k: "SCENE")
    buf = io.StringIO()
    m._print_splash_banner(interactive=True, stream=buf)
    assert buf.getvalue() == "\nSCENE\n"


def test_print_splash_banner_is_noop_when_not_interactive(monkeypatch) -> None:
    import imp.cli.main as m
    monkeypatch.setattr(m.splash, "splash_block", lambda **_k: "SCENE")
    buf = io.StringIO()
    m._print_splash_banner(interactive=False, stream=buf)
    assert buf.getvalue() == ""


def test_print_splash_banner_skips_empty_scene(monkeypatch) -> None:
    import imp.cli.main as m
    monkeypatch.setattr(m.splash, "splash_block", lambda **_k: "")
    buf = io.StringIO()
    m._print_splash_banner(interactive=True, stream=buf)
    assert buf.getvalue() == ""


def test_crescent_paints_moon_pixels_around_the_anchor() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._crescent(g, 24, 4)
    assert g[4][24] == splash._P["moon"]  # (0,0) offset
    assert g[2][24] == splash._P["moon"]  # (0,-2) offset


def test_moon_glow_full_disc_then_crescent_carves_part_away() -> None:
    splash._use_theme("dark")
    full = splash._make_grid()
    splash._moon_glow(full, 24, 5, 4, False)
    cres = splash._make_grid()
    splash._moon_glow(cres, 24, 5, 4, True)
    full_moon = sum(1 for row in full for c in row if c == splash._P["moon"])
    cres_moon = sum(1 for row in cres for c in row if c == splash._P["moon"])
    assert full_moon > 0
    assert cres_moon < full_moon


def test_cloud_fills_its_centre_with_the_given_colour() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._cloud(g, 24, 8, splash._P["steam"])
    assert g[8][24] == splash._P["steam"]


def test_meteor_head_is_bright_and_tail_trends_to_star() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._meteor(g, 19, 3, 5)
    assert g[3][19] == splash._P["bright"]  # spark head at (x,y)
    assert g[1][21] == splash._P["star"]    # tail pixel k=2 (t=0.4) -> star


def test_cosmic_batch_a_scenes_are_wired_in_gallery_order() -> None:
    expect = [
        (splash._s_crescent_stars, "the familiar awakens.", splash.ANY),
        (splash._s_crescent_perch, "perched and ready.", splash.ANY),
        (splash._s_full_moon_cloud, "what are we building tonight?", splash.NIGHT),
        (splash._s_meteor_shower, "make a wish.", splash.ANY),
    ]
    for entry in expect:
        assert entry in splash.SCENES
    names = [b.__name__ for b, _p, _w in splash.SCENES]
    order = ["_s_crescent_stars", "_s_crescent_perch", "_s_full_moon_cloud",
             "_s_meteor_shower", "_s_constellation"]
    positions = [names.index(n) for n in order]
    assert positions == sorted(positions)


def test_full_moon_scene_is_eligible_only_at_night(monkeypatch) -> None:
    idx = next(i for i, (_b, _p, w) in enumerate(splash.SCENES) if w == splash.NIGHT)
    monkeypatch.setattr(splash, "_now_hour", lambda: 23)
    assert idx in splash._eligible()
    monkeypatch.setattr(splash, "_now_hour", lambda: 12)
    assert idx not in splash._eligible()


def test_planet_paints_a_body_and_a_ring() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._planet(g, 24, 7, 3)
    assert any(c == splash._P["planetA"] for row in g for c in row)
    assert any(c == splash._P["ring"] for row in g for c in row)


def test_mountains_cap_snow_above_the_mid_fill() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._mountains(g, 14, 15)
    snow = [y for y, row in enumerate(g) for c in row if c == splash._P["snow"]]
    mid = [y for y, row in enumerate(g) for c in row if c == splash._P["mtnMid"]]
    assert snow and mid
    assert min(snow) <= min(mid)


def test_aurora_uses_three_hues_confined_to_the_accent_columns() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._aurora(g)
    painted = [(x, c) for _y, row in enumerate(g) for x, c in enumerate(row) if c is not None]
    hues = {c for _x, c in painted}
    assert splash._P["green"] in hues
    assert splash._P["teal"] in hues
    assert splash._P["violet"] in hues
    assert all(x >= 18 for x, _c in painted)


def test_comet_scene_paints_a_bright_head() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._s_comet(g)
    assert g[2][28] == splash._P["bright"]  # head disc centred at (28,2)


def test_cosmic_batch_b_scenes_are_wired_in_gallery_order() -> None:
    expect = [
        (splash._s_ringed_planet, "a world, from nothing.", splash.ANY),
        (splash._s_mountain_moon, "welcome back.", splash.ANY),
        (splash._s_comet, "something wicked this way ships.", splash.ANY),
        (splash._s_aurora, "the sky's showing off — let's match it.", splash.ANY),
    ]
    for entry in expect:
        assert entry in splash.SCENES
    names = [b.__name__ for b, _p, _w in splash.SCENES]
    assert names.index("_s_constellation") < names.index("_s_ringed_planet")
    assert names.index("_s_ringed_planet") < names.index("_s_starfield")


def test_galaxy_paints_a_bright_core_and_a_mirrored_arm() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._galaxy(g, 24, 6)
    assert g[6][24] == splash._P["bright"]  # core disc centre
    assert any(c == splash._P["port"] for row in g for c in row)  # mirrored arm hue


def test_void_hole_paints_a_dark_core_inside_an_ember_glow() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._void_hole(g, 23, 7)
    assert any(c == splash._P["voidCore"] for row in g for c in row)
    assert any(c == splash._P["ember"] for row in g for c in row)


def test_summoning_circle_scene_paints_a_portal_ring_and_runes() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._s_summoning_circle(g)
    assert any(c == splash._P["port"] for row in g for c in row)
    assert any(c == splash._P["rune"] for row in g for c in row)


def test_cosmic_batch_c_scenes_complete_the_thirteen_scene_pool() -> None:
    expect = [
        (splash._s_galaxy, "lost in the stack? i know the way.", splash.ANY),
        (splash._s_void, "conjured from the void.", splash.ANY),
        (splash._s_summoning_circle, "the circle is drawn.", splash.ANY),
    ]
    for entry in expect:
        assert entry in splash.SCENES
    assert len(splash.SCENES) >= 13
    names = [b.__name__ for b, _p, _w in splash.SCENES]
    assert names[:13] == [
        "_s_crescent_stars", "_s_crescent_perch", "_s_full_moon_cloud", "_s_meteor_shower",
        "_s_constellation", "_s_ringed_planet", "_s_mountain_moon", "_s_comet", "_s_aurora",
        "_s_galaxy", "_s_void", "_s_starfield", "_s_summoning_circle",
    ]


def test_ritual_vessel_scenes_are_wired_and_props_paint() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._portal(g, 24, 7)
    assert g[2][24] == splash._P["portI"]   # spark(cx,cy-5) head at (24,2)
    g = splash._make_grid()
    splash._orb(g, 24, 7, 4)
    assert g[5][22] == splash._P["orbHi"]    # px(cx-2,cy-2) highlight
    g = splash._make_grid()
    splash._cauldron(g, 24, 8, 9)
    assert g[0][24] == splash._P["steam"]    # px(cx,top_y-8) steam plume top
    g = splash._make_grid()
    splash._spellbook(g, 23, 11)
    assert g[6][23] == splash._P["rune"]     # spark(cx,by-5) sigil at (23,6)
    expect = [
        (splash._s_portal, "the portal is open. step through.", splash.ANY),
        (splash._s_crystal_ball, "gaze in — what shall we build?", splash.ANY),
        (splash._s_cauldron, "stir the cauldron — what's brewing?", splash.ANY),
        (splash._s_grimoire, "the grimoire is open.", splash.ANY),
    ]
    for entry in expect:
        assert entry in splash.SCENES
    names = [b.__name__ for b, _p, _w in splash.SCENES]
    idx = [names.index(b.__name__) for b, _p, _w in expect]
    assert idx == sorted(idx)
    assert names.index("_s_summoning_circle") < idx[0]


def test_flame_and_sigil_scenes_are_wired_and_props_paint() -> None:
    splash._use_theme("dark")
    g = splash._make_grid()
    splash._lantern(g, 21, 13)
    assert g[12][21] == splash._P["lanMetal"]  # rect(x,by-1,4,1) base at (21,12)
    g = splash._make_grid()
    splash._candle(g, 21, 13)
    assert g[7][21] == splash._P["W"]          # px(x,by-6) wick glint at (21,7)
    g = splash._make_grid()
    splash._torch(g, 20, 13)
    assert g[4][20] == splash._P["W"]          # px(x,by-9) flame tip at (20,4)
    g = splash._make_grid()
    splash._rune_sigil(g, 24, 7)
    assert g[7][24] == splash._P["rune"]        # px(cx,cy) centre rune
    expect = [
        (splash._s_lantern, "the lantern's lit. lead the way.", splash.ANY),
        (splash._s_twin_candles, "whisper your wish.", splash.ANY),
        (splash._s_twin_torches, "let's raise a little hell.", splash.ANY),
        (splash._s_rune_sigil, "the runes are ready.", splash.ANY),
    ]
    for entry in expect:
        assert entry in splash.SCENES
    names = [b.__name__ for b, _p, _w in splash.SCENES]
    idx = [names.index(b.__name__) for b, _p, _w in expect]
    assert idx == sorted(idx)
    assert names.index("_s_grimoire") < idx[0]
