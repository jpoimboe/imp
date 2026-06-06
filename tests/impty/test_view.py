from __future__ import annotations

import pyte

from impty import view


def _screen(*chunks: bytes, cols: int = 40, rows: int = 8) -> pyte.HistoryScreen:
    screen = pyte.HistoryScreen(cols, rows, history=200, ratio=0.5)
    stream = pyte.ByteStream(screen)
    for chunk in chunks:
        stream.feed(chunk)
    return screen


def test_viewport_text_renders_lines_without_trailing_pad() -> None:
    screen = _screen(b"hello\r\nworld\r\n")
    text = view.viewport_text(screen)
    lines = text.split("\n")
    assert lines[0] == "hello"
    assert lines[1] == "world"
    # no trailing spaces from the 40-col padding
    assert text == text.replace(" \n", "\n")


def test_viewport_text_collapses_self_erasing_status_line() -> None:
    # \r\x1b[K rewrites the line in place; only the final state should survive.
    screen = _screen(b"working |", b"\r\x1b[Kworking /", b"\r\x1b[KReady.\r\n")
    assert "working" not in view.viewport_text(screen)
    assert "Ready." in view.viewport_text(screen)


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


def test_transcript_stitches_history_and_viewport_in_order() -> None:
    # Emit more lines than the 8-row viewport so some scroll into history.
    chunks = [f"line {i:02d}\r\n".encode() for i in range(1, 21)]
    screen = _screen(*chunks, cols=40, rows=8)
    transcript = view.transcript_text(screen)
    lines = transcript.split("\n")
    assert lines[0] == "line 01"          # came from history.top
    assert lines[-1] == "line 20"         # came from the viewport
    assert len(lines) == 20               # no gaps, no double-counting


def test_transcript_limit_returns_last_n_lines() -> None:
    chunks = [f"line {i:02d}\r\n".encode() for i in range(1, 21)]
    screen = _screen(*chunks, cols=40, rows=8)
    lines = view.transcript_text(screen, limit=3).split("\n")
    assert lines == ["line 18", "line 19", "line 20"]


def test_is_alt_screen_detects_1049() -> None:
    plain = _screen(b"hi")
    assert view.is_alt_screen(plain) is False
    alt = _screen(b"\x1b[?1049h")        # enter alternate screen
    assert view.is_alt_screen(alt) is True
