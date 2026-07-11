from __future__ import annotations

import io

from imp.cli.terminal_title import (
    GLYPH_BUSY,
    GLYPH_IDLE,
    TerminalTitle,
)


class _TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


class _Pipe(io.StringIO):
    def isatty(self) -> bool:
        return False


def test_default_glyphs_match_spec() -> None:
    assert GLYPH_BUSY == "๏"
    assert GLYPH_IDLE == "ѱ"


def test_init_sets_busy_title_on_tty() -> None:
    s = _TTY()
    TerminalTitle(s, "nagi22", "My Quest")
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · My Quest\x07"


def test_init_with_initial_idle_emits_idle_glyph_on_tty() -> None:
    s = _TTY()
    TerminalTitle(s, "nagi22", "My Quest", initial="idle")
    assert s.getvalue() == f"\x1b]2;{GLYPH_IDLE} imp · nagi22 · My Quest\x07"


def test_idle_emits_idle_glyph_with_full_name() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Quest")
    s.truncate(0)
    s.seek(0)
    title.idle()
    assert s.getvalue() == f"\x1b]2;{GLYPH_IDLE} imp · nagi22 · My Quest\x07"


def test_busy_overwrites_idle() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Quest")
    title.idle()
    s.truncate(0)
    s.seek(0)
    title.busy()
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · My Quest\x07"


def test_empty_name_drops_separator() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "")
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22\x07"
    s.truncate(0)
    s.seek(0)
    title.idle()
    assert s.getvalue() == f"\x1b]2;{GLYPH_IDLE} imp · nagi22\x07"


def test_reset_clears_title() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Quest")
    s.truncate(0)
    s.seek(0)
    title.reset()
    assert s.getvalue() == "\x1b]2;\x07"


def test_inert_on_non_tty_stream() -> None:
    s = _Pipe()
    title = TerminalTitle(s, "nagi22", "My Quest")
    title.busy()
    title.idle()
    title.reset()
    assert s.getvalue() == ""


def test_update_name_emits_new_name_under_busy_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · foo\x07"


def test_update_name_same_name_is_noop() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Quest")
    s.truncate(0)
    s.seek(0)
    title.update_name("My Quest")
    assert s.getvalue() == ""


def test_update_name_preserves_idle_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.idle()
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == f"\x1b]2;{GLYPH_IDLE} imp · nagi22 · foo\x07"


def test_update_name_inert_on_non_tty() -> None:
    s = _Pipe()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.update_name("foo")
    assert s.getvalue() == ""


def test_update_name_after_reset_uses_busy_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.reset()
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · foo\x07"


def test_set_hold_glyph_emits_hold_glyph_when_busy() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Quest")
    s.truncate(0)
    s.seek(0)
    title.set_hold_glyph("⛙")
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · My Quest\x07"


def test_set_hold_glyph_none_returns_to_busy_dot() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Quest")
    title.set_hold_glyph("⛙")
    s.truncate(0)
    s.seek(0)
    title.set_hold_glyph(None)
    assert s.getvalue() == "\x1b]2;๏ imp · nagi22 · My Quest\x07"


def test_idle_overrides_active_hold_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Quest")
    title.set_hold_glyph("⛙")
    s.truncate(0)
    s.seek(0)
    title.idle()
    assert s.getvalue() == f"\x1b]2;{GLYPH_IDLE} imp · nagi22 · My Quest\x07"


def test_busy_restores_hold_glyph_after_idle() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Quest")
    title.set_hold_glyph("⛙")
    title.idle()
    s.truncate(0)
    s.seek(0)
    title.busy()
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · My Quest\x07"


def test_update_name_preserves_active_hold_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.set_hold_glyph("⛙")
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · foo\x07"


def test_busy_restores_hold_glyph_set_while_idle() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "My Quest")
    title.idle()
    title.set_hold_glyph("⛙")
    s.truncate(0)
    s.seek(0)
    title.busy()
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · My Quest\x07"


def test_update_name_after_reset_reemits_under_hold_glyph() -> None:
    s = _TTY()
    title = TerminalTitle(s, "nagi22", "Old Name")
    title.set_hold_glyph("⛙")
    title.reset()
    s.truncate(0)
    s.seek(0)
    title.update_name("foo")
    assert s.getvalue() == "\x1b]2;⛙ imp · nagi22 · foo\x07"
