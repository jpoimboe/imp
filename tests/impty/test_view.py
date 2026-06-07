from __future__ import annotations

import pyte

from impty import view
from impty.screen import Regime, TerminalScreen


def _screen(*chunks: bytes, cols: int = 40, rows: int = 8) -> TerminalScreen:
    screen = TerminalScreen(cols, rows, history=200, ratio=0.5)
    stream = pyte.ByteStream(screen)
    for chunk in chunks:
        stream.feed(chunk)
    return screen


def test_cursor_pos_after_prompt() -> None:
    screen = _screen(b"Name? ")
    assert view.cursor_pos(screen) == (6, 0)  # parked right after the prompt


def test_cell_at_carries_style() -> None:
    screen = _screen(b"\x1b[1;31mE\x1b[0mok")
    cell = view.cell_at(screen, 0, 0)
    assert cell.data == "E"
    assert cell.fg == "red"
    assert cell.bold is True
    plain = view.cell_at(screen, 1, 0)
    assert plain.data == "o"
    assert plain.bold is False


def test_is_alt_screen_detects_1049() -> None:
    plain = _screen(b"hi")
    assert view.is_alt_screen(plain) is False
    alt = _screen(b"\x1b[?1049h")        # enter alternate screen
    assert view.is_alt_screen(alt) is True


def test_lines_windowed_aligns_with_cell_over_blank_rows() -> None:
    # Scrollback present + a prompt near the top of an otherwise-blank screen.
    # lines(limit=rows) must be EXACTLY rows entries with NO trailing-blank
    # stripping, and row y must equal cell(.,y)'s row — the alignment invariant.
    screen = _screen(cols=10, rows=6)
    stream = pyte.ByteStream(screen)
    for i in range(1, 13):
        stream.feed(f"old {i}\r\n".encode())   # scroll lines into history
    stream.feed(b"\x1b[2J\x1b[HName? ")          # clear, home, prompt near top
    win = view.lines(screen, limit=6)
    assert len(win) == 6                          # exactly rows, blanks padded
    assert win[0] == "Name?"                      # internal rstrip only
    assert not any("old" in line for line in win) # scrollback NOT pulled into window
    for y in range(6):
        assert win[y] == screen.display[y].rstrip()   # row-for-row with the screen
    assert view.cell_at(screen, 0, 0).data == "N"     # cell agrees on (0,0)


def test_lines_unbounded_includes_history_and_trims_trailing_blanks() -> None:
    screen = _screen(cols=10, rows=6)
    stream = pyte.ByteStream(screen)
    for i in range(1, 13):
        stream.feed(f"old {i}\r\n".encode())
    stream.feed(b"\x1b[2J\x1b[HName? ")
    full = view.lines(screen)
    assert "old 1" in full                  # scrollback included
    assert full[-1] == "Name?"              # trailing blank screen rows trimmed


def test_lines_in_screen_regime_has_no_scrollback() -> None:
    screen = _screen(cols=10, rows=4)
    stream = pyte.ByteStream(screen)
    for i in range(1, 8):
        stream.feed(f"L{i}\r\n".encode())   # populate history.top
    stream.feed(b"\x1b[?1049h\x1b[2J\x1b[HALT")
    assert view.regime(screen) is Regime.SCREEN
    assert view.is_alt_screen(screen) is True
    assert view.lines(screen) == ["ALT"]    # only the alt frame, no L* history


def test_cell_blank_for_undrawn_column() -> None:
    screen = _screen(b"hi", cols=10, rows=4)
    cell = view.cell_at(screen, 5, 0)       # column 5 never drawn
    assert cell.data == " "
    assert cell.bold is False


def test_lines_collapses_self_erasing_status_line() -> None:
    # \r\x1b[K rewrites the line in place; only the final state survives.
    screen = _screen(b"working |", b"\r\x1b[Kworking /", b"\r\x1b[KReady.\r\n")
    result = view.lines(screen)
    assert not any("working" in line for line in result)
    assert any("Ready." in line for line in result)
