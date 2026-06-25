from __future__ import annotations

import io

from imp.cli.terminal_title import (
    GLYPH_WAITING,
    GLYPH_WORKING,
    TerminalTitle,
)


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_default_glyphs_match_spec() -> None:
    assert GLYPH_WORKING == "·"
    assert GLYPH_WAITING == "Ⲫ"


def test_init_sets_working_title_on_tty() -> None:
    s = _TTY()
    TerminalTitle(s, "My Change")
    assert s.getvalue() == f"\x1b]2;{GLYPH_WORKING} imp — My Change\x07"


def test_waiting_emits_waiting_glyph_with_full_name() -> None:
    s = _TTY()
    title = TerminalTitle(s, "My Change")
    s.truncate(0)
    s.seek(0)
    title.waiting()
    assert s.getvalue() == f"\x1b]2;{GLYPH_WAITING} imp — My Change\x07"


def test_working_overwrites_waiting() -> None:
    s = _TTY()
    title = TerminalTitle(s, "My Change")
    title.waiting()
    s.truncate(0)
    s.seek(0)
    title.working()
    assert s.getvalue() == f"\x1b]2;{GLYPH_WORKING} imp — My Change\x07"


def test_reset_clears_title() -> None:
    s = _TTY()
    title = TerminalTitle(s, "My Change")
    s.truncate(0)
    s.seek(0)
    title.reset()
    assert s.getvalue() == "\x1b]2;\x07"


def test_inert_on_non_tty_stream() -> None:
    s = _Pipe()
    title = TerminalTitle(s, "My Change")
    title.working()
    title.waiting()
    title.reset()
    assert s.getvalue() == ""
