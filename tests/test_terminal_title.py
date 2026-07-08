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
    assert GLYPH_WORKING == "๏"
    assert GLYPH_WAITING == "ѱ"


def test_init_sets_working_title_on_tty() -> None:
    s = _TTY()
    TerminalTitle(s, "nagi22", "My Change")
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · My Change\x07"


def test_waiting_emits_waiting_glyph_with_full_name() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Change")
    s.truncate(0)
    s.seek(0)
    title.waiting()
    assert s.getvalue() == f"\x1b]2;{GLYPH_WAITING} imp · nagi22 · My Change\x07"


def test_working_overwrites_waiting() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Change")
    title.waiting()
    s.truncate(0)
    s.seek(0)
    title.working()
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · My Change\x07"


def test_empty_name_drops_separator() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "")
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22\x07"
    s.truncate(0)
    s.seek(0)
    title.waiting()
    assert s.getvalue() == f"\x1b]2;{GLYPH_WAITING} imp · nagi22\x07"


def test_reset_clears_title() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Change")
    s.truncate(0)
    s.seek(0)
    title.reset()
    assert s.getvalue() == "\x1b]2;\x07"


def test_inert_on_non_tty_stream() -> None:
    s = _Pipe()
    title = TerminalTitle(s, "nagi22", "My Change")
    title.working()
    title.waiting()
    title.reset()
    assert s.getvalue() == ""


def test_update_name_emits_new_name_under_working_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · foo\x07"


def test_update_name_same_name_is_noop() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Change")
    s.truncate(0)
    s.seek(0)
    title.update_name("My Change")
    assert s.getvalue() == ""


def test_update_name_preserves_waiting_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.waiting()
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == f"\x1b]2;{GLYPH_WAITING} imp · nagi22 · foo\x07"


def test_update_name_inert_on_non_tty() -> None:
    s = _Pipe()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.update_name("foo")
    assert s.getvalue() == ""


def test_update_name_after_reset_uses_working_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.reset()
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · foo\x07"


def test_set_hold_glyph_emits_hold_glyph_when_working() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Change")
    s.truncate(0)
    s.seek(0)
    title.set_hold_glyph("⛙")
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · My Change\x07"


def test_set_hold_glyph_none_returns_to_working_dot() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Change")
    title.set_hold_glyph("⛙")
    s.truncate(0)
    s.seek(0)
    title.set_hold_glyph(None)
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · My Change\x07"


def test_waiting_overrides_active_hold_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Change")
    title.set_hold_glyph("⛙")
    s.truncate(0)
    s.seek(0)
    title.waiting()
    assert s.getvalue() == f"\x1b]2;{GLYPH_WAITING} imp · nagi22 · My Change\x07"


def test_working_restores_hold_glyph_after_waiting() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Change")
    title.set_hold_glyph("⛙")
    title.waiting()
    s.truncate(0)
    s.seek(0)
    title.working()
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · My Change\x07"


def test_update_name_preserves_active_hold_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.set_hold_glyph("⛙")
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · foo\x07"


def test_working_restores_hold_glyph_set_while_waiting() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Change")
    title.waiting()
    title.set_hold_glyph("⛙")
    s.truncate(0)
    s.seek(0)
    title.working()
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · My Change\x07"


def test_update_name_after_reset_reemits_under_hold_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.set_hold_glyph("⛙")
    title.reset()
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · foo\x07"
