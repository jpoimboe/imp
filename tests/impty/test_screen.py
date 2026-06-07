from __future__ import annotations

import pyte

from impty.screen import Regime, TerminalScreen


def _ts(cols: int = 20, rows: int = 6, history: int = 100):
    screen = TerminalScreen(cols, rows, history=history, ratio=0.5)
    return screen, pyte.ByteStream(screen)


def test_starts_in_scroll_regime() -> None:
    screen, _ = _ts()
    assert screen.regime is Regime.SCROLL


def test_enter_alt_sets_screen_regime_and_homes_cursor() -> None:
    screen, stream = _ts()
    stream.feed(b"hello")              # cursor now at (5, 0)
    stream.feed(b"\x1b[?1049h")        # enter alternate screen
    assert screen.regime is Regime.SCREEN
    assert (screen.cursor.x, screen.cursor.y) == (0, 0)


def test_other_modes_in_the_same_csi_are_still_recorded() -> None:
    # ESC[?25;1049h batches DECTCEM(25) + alt(1049); the override must still
    # delegate to super() so cursor-visibility is applied alongside the swap.
    screen, stream = _ts()
    stream.feed(b"\x1b[?25;1049h")
    assert screen.regime is Regime.SCREEN
    assert screen.cursor.hidden is False  # DECTCEM set => cursor visible


def test_reply_shim_forwards_to_callback() -> None:
    screen, _ = _ts()
    got: list[str] = []
    screen.reply = got.append
    screen.write_process_input("\x1b[1;1R")
    assert got == ["\x1b[1;1R"]


def test_reply_shim_is_noop_without_callback() -> None:
    screen, _ = _ts()
    screen.write_process_input("x")   # must not raise


def test_alt_exit_restores_prealt_buffer_and_cursor() -> None:
    # Concern C: rebind (not clear-in-place) — pre-alt buffer AND cursor restored.
    screen, stream = _ts()
    stream.feed(b"PRE-ALT")                  # row 0 = "PRE-ALT", cursor at (7, 0)
    stream.feed(b"\x1b[?1049h")              # enter alt
    stream.feed(b"\x1b[2J\x1b[HALT-CONTENT") # clobber the alt grid
    stream.feed(b"\x1b[?1049l")              # exit alt
    assert screen.regime is Regime.SCROLL
    assert screen.display[0].rstrip() == "PRE-ALT"
    assert (screen.cursor.x, screen.cursor.y) == (7, 0)


def test_alt_does_not_pollute_history_top_or_bottom() -> None:
    # Concern B: suppress BOTH index() (LF -> history.top) and reverse_index()
    # (ESC M at top margin -> history.bottom).
    screen, stream = _ts(cols=10, rows=4)
    stream.feed(b"a\r\nb\r\nc\r\nd\r\ne\r\n")   # scroll some lines into history.top
    top0, bot0 = len(screen.history.top), len(screen.history.bottom)
    assert top0 > 0                              # baseline is non-empty (meaningful)
    stream.feed(b"\x1b[?1049h")
    stream.feed(b"\x1b[4;1H")                    # cursor to bottom margin (row 4)
    stream.feed(b"\n" * 10)                      # LF/IND at bottom -> would feed top
    stream.feed(b"\x1b[1;1H")                    # cursor to top margin (row 1)
    stream.feed(b"\x1bM" * 10)                   # RI at top -> would feed bottom
    stream.feed(b"\x1b[?1049l")
    assert len(screen.history.top) == top0
    assert len(screen.history.bottom) == bot0


def test_double_enter_does_not_clobber_saved_buffer() -> None:
    screen, stream = _ts()
    stream.feed(b"PRE")
    stream.feed(b"\x1b[?1049h")
    stream.feed(b"ALT-ONE")
    stream.feed(b"\x1b[?1049h")     # spurious second enter — must NOT re-save
    stream.feed(b"\x1b[?1049l")
    assert screen.display[0].rstrip() == "PRE"


def test_spurious_exit_is_noop() -> None:
    screen, stream = _ts()
    stream.feed(b"PLAIN")
    stream.feed(b"\x1b[?1049l")     # exit with nothing saved — must NOT restore None
    assert screen.regime is Regime.SCROLL
    assert screen.display[0].rstrip() == "PLAIN"


def test_resize_during_alt_resizes_saved_buffer_and_clamps_cursor() -> None:
    # Concern D: shrink while in alt; saved buffer must be clipped to the new
    # dims and the saved cursor clamped, so the restored grid stays in bounds.
    screen, stream = _ts(cols=20, rows=8)
    stream.feed(b"\x1b[8;1H")        # cursor to bottom row
    stream.feed(b"X" * 20)           # fill the row full width (no scroll: exactly cols)
    stream.feed(b"\x1b[?1049h")      # enter alt: saves the 20x8 buffer + cursor copy
    assert (screen._saved_columns, screen._saved_lines) == (20, 8)
    screen.resize(lines=4, columns=10)   # shrink mid-alt
    assert screen._saved_columns == 10 and screen._saved_lines == 4
    # saved buffer shape ACTUALLY changed: no column keys >= 10, and <= 4 rows.
    assert all(max(row, default=-1) < 10 for row in screen._saved_buffer.values())
    assert len(screen._saved_buffer) <= 4
    stream.feed(b"\x1b[?1049l")          # exit -> restore at the new dims
    assert screen.columns == 10 and screen.lines == 4
    assert len(screen.display) == 4
    assert all(len(line) == 10 for line in screen.display)
    # The cursor assertion catches an un-clamped saved cursor (cell/display clamp
    # independently and would pass regardless).
    assert 0 <= screen.cursor.x <= 9 and 0 <= screen.cursor.y <= 3
