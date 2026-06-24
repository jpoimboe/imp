from __future__ import annotations

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
