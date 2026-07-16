"""Tests for vi-mode display-row motion (pure function, no terminal required)."""
from __future__ import annotations

from prompt_toolkit.utils import get_cwidth

from imp.cli.vi_motion import _build_rows, display_line_move

# ---------------------------------------------------------------------------
# AC 1 — row boundaries match prompt_toolkit's actual wrapping
# ---------------------------------------------------------------------------


def test_single_line_wraps_to_rows() -> None:
    # "abcdefg" with w=3 → rows "abc"(0-2), "def"(3-5), "g"(6)
    # Down from row 0 lands on offset 3 (start of "def"), then 6 (start of "g").
    text = "abcdefg"
    c1, _ = display_line_move(text, 0, w=3, count=1, direction=1, preferred_x=None)
    assert c1 == 3
    c2, _ = display_line_move(text, 3, w=3, count=1, direction=1, preferred_x=None)
    assert c2 == 6


def test_hard_newline_mixed_with_wrap() -> None:
    # "abc\nde" with w=2:
    #   "abc" wraps → row "ab"(0-1), row "c"(2-3)
    #   "de" fits   → row "de"(4-6)
    # Down from cursor=0 (row "ab", x=0) → cursor=2 (start of row "c")
    # Down from cursor=2 (row "c")       → cursor=4 (start of row "de")
    text = "abc\nde"
    c1, _ = display_line_move(text, 0, w=2, count=1, direction=1, preferred_x=None)
    assert c1 == 2
    c2, _ = display_line_move(text, 2, w=2, count=1, direction=1, preferred_x=None)
    assert c2 == 4


def test_empty_logical_line() -> None:
    # "a\n\nb": row "a"(0-1), row ""(2-2), row "b"(3-4)
    # Moving down past the empty row and back up must land at correct offsets.
    text = "a\n\nb"
    c1, _ = display_line_move(text, 1, w=5, count=1, direction=1, preferred_x=0)
    assert c1 == 2  # cursor on empty row (start == end)
    c2, _ = display_line_move(text, 2, w=5, count=1, direction=1, preferred_x=0)
    assert c2 == 3  # start of "b"
    # Moving back up from "b" should reach the empty row
    c3, _ = display_line_move(text, 3, w=5, count=1, direction=-1, preferred_x=0)
    assert c3 == 2


def test_double_width_row_split() -> None:
    # "你好" with w=3: '你'(width 2) fits; '好'(width 2) would push to 4 > 3 → new row.
    # Row 0: "你"(offset 0-0), row 1: "好"(offset 1-1) [Python str offsets, not bytes].
    assert get_cwidth("你") == 2
    assert get_cwidth("好") == 2
    text = "你好"
    c, _ = display_line_move(text, 0, w=3, count=1, direction=1, preferred_x=None)
    assert c == 1  # start of '好'


def test_double_width_x_alignment() -> None:
    # "你好" w=3.  preferred_x=1 falls *inside* the double-width '好' in row 1;
    # cursor must land at the start of '好' (offset 1) because 0+2 > 1 breaks immediately.
    text = "你好"
    c, _ = display_line_move(text, 0, w=3, count=1, direction=1, preferred_x=1)
    assert c == 1
    # preferred_x=2 (exactly one full double-width unit) → past '好' → end of row
    c2, _ = display_line_move(text, 0, w=3, count=1, direction=1, preferred_x=2)
    assert c2 == 2  # end of "好": valid end-of-line insertion point


# ---------------------------------------------------------------------------
# AC 2 — preferred visual column preserved across rows of differing length
# ---------------------------------------------------------------------------


def test_preferred_x_preserved_across_short_row() -> None:
    # "abcde\nxy\nfghij" — three rows, middle row is short.
    # Start at cursor=4 ('e', x=4) with preferred_x=None.
    # Move down: lands at end of "xy" (cursor=8), preferred_x pinned at 4.
    # Move down again: lands at x=4 in "fghij" (cursor=13).
    # Move up 2: round-trips back to cursor=4.
    text = "abcde\nxy\nfghij"
    c1, px1 = display_line_move(text, 4, w=10, count=1, direction=1, preferred_x=None)
    assert c1 == 8   # end of "xy" (clamped by short row)
    assert px1 == 4  # x of 'e' preserved

    c2, px2 = display_line_move(text, c1, w=10, count=1, direction=1, preferred_x=px1)
    assert c2 == 13  # offset 9+4 = 13 (after 'i' in "fghij")
    assert px2 == 4

    c3, _ = display_line_move(text, c2, w=10, count=2, direction=-1, preferred_x=px2)
    assert c3 == 4   # round-trip back to original column


def test_preferred_x_none_recomputes_from_cursor() -> None:
    # cursor=7 ('y', x=1 in row "xy").  preferred_x=None → computes x=1.
    # Moving up one row into "abcde": cursor_at_x(1) = offset 1 ('b').
    text = "abcde\nxy\nfghij"
    c, px = display_line_move(text, 7, w=10, count=1, direction=-1, preferred_x=None)
    assert c == 1    # 'b' in "abcde", x=1
    assert px == 1   # recomputed from cursor position


# ---------------------------------------------------------------------------
# AC 3 — count honored; edges clamped
# ---------------------------------------------------------------------------


def test_count_3_moves_three_rows() -> None:
    # Four rows via hard newlines; count=3 from row 0 → row 3.
    text = "a\nb\nc\nd"
    c, px = display_line_move(text, 0, w=10, count=3, direction=1, preferred_x=None)
    assert c == 6    # start of 'd'
    assert px == 0


def test_clamp_at_bottom() -> None:
    # count=100 down from row 0 of a 3-row buffer must land exactly on last row.
    text = "a\nb\nc"
    c, _ = display_line_move(text, 0, w=10, count=100, direction=1, preferred_x=None)
    assert c == 4    # start of 'c'


def test_clamp_at_top() -> None:
    # count=100 up from last row must land exactly on row 0.
    text = "a\nb\nc"
    c, _ = display_line_move(text, 4, w=10, count=100, direction=-1, preferred_x=None)
    assert c == 0    # start of 'a'


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_buffer_is_noop() -> None:
    c, px = display_line_move("", 0, w=5, count=1, direction=1, preferred_x=None)
    assert c == 0
    assert px == 0


def test_cursor_at_end_of_buffer() -> None:
    # cursor == len(text): must land on last row and stay clamped there.
    text = "hello"
    c, px = display_line_move(text, 5, w=10, count=1, direction=1, preferred_x=None)
    assert c == 5    # already at bottom, stays
    assert px == 5   # x after all 5 chars


def test_soft_wrap_boundary_cursor_belongs_to_next_row() -> None:
    # "abcdef" w=3: rows "abc"(0-2), "def"(3-5).
    # cursor=3 is the soft-wrap boundary — must be in row 1 ("def"), not row 0 ("abc").
    # Moving up from cursor=3 should land on row 0.
    text = "abcdef"
    c, _ = display_line_move(text, 3, w=3, count=1, direction=-1, preferred_x=None)
    assert c == 0    # x=0 of row 0 (cursor was at x=0 of row 1)


def test_j_soft_wrap_no_row_skip() -> None:
    # "XYZ\nabcdef" w=3:
    #   Row 0: "XYZ"  offsets 0-2, last_in_line=True
    #   Row 1: "abc"  offsets 4-6, last_in_line=False (soft-wrap)
    #   Row 2: "def"  offsets 7-9, last_in_line=True
    # cursor=3 (end of "XYZ", x=3). j must land in row 1, not skip to row 2.
    text = "XYZ\nabcdef"
    c1, px1 = display_line_move(text, 3, w=3, count=1, direction=1, preferred_x=None)
    assert c1 == 6    # clamped to last char position of soft-wrap row "abc"
    assert px1 == 3   # visual x of cursor=3 in "XYZ"

    # Second j threads preferred_x back and advances to row 2 ("def"), not beyond.
    c2, _ = display_line_move(text, c1, w=3, count=1, direction=1, preferred_x=px1)
    assert c2 == 10   # end-of-line insertion point of last row "def"


def test_narrow_terminal_wide_chars_no_infinite_loop() -> None:
    # w=1 is narrower than a double-width CJK char (cwidth=2); the zero-progress
    # guard must prevent an infinite loop and give each char its own row.
    rows = _build_rows("你好", w=1)
    assert len(rows) == 2
    assert rows[0].start == 0 and rows[0].end == 1   # '你' alone
    assert rows[1].start == 1 and rows[1].end == 2   # '好' alone
    # Moving j from '你' lands on '好' (offset 1)
    c, _ = display_line_move("你好", 0, w=1, count=1, direction=1, preferred_x=None)
    assert c == 1
