# tests/test_palette.py
from __future__ import annotations

import io

import pytest

from imp.cli import palette as pal


def test_ansi_disabled_returns_plain() -> None:
    assert pal.ansi("hi", "red", enabled=False) == "hi"


def test_ansi_enabled_wraps_and_resets_once() -> None:
    assert pal.ansi("hi", "bold", "magenta", enabled=True) == "\x1b[1m\x1b[35mhi\x1b[0m"


class _TTY:
    def isatty(self) -> bool:
        return True


def test_should_use_color_requires_tty_and_no_NO_COLOR(monkeypatch) -> None:
    for var in ("NO_COLOR", "CLICOLOR_FORCE", "FORCE_COLOR"):
        monkeypatch.delenv(var, raising=False)
    assert pal.should_use_color(io.StringIO()) is False     # not a tty
    assert pal.should_use_color(_TTY()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert pal.should_use_color(_TTY()) is False


@pytest.mark.parametrize("force_var", ["CLICOLOR_FORCE", "FORCE_COLOR"])
def test_should_use_color_force_var_overrides_pipe(monkeypatch, force_var) -> None:
    # Force vars color even a non-tty (e.g. `imp log | less -R`)...
    for var in ("NO_COLOR", "CLICOLOR_FORCE", "FORCE_COLOR"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(force_var, "1")
    assert pal.should_use_color(io.StringIO()) is True
    # ...but NO_COLOR still wins.
    monkeypatch.setenv("NO_COLOR", "1")
    assert pal.should_use_color(io.StringIO()) is False


@pytest.mark.parametrize("action,expected", [
    ({"verb": "continue"}, "green"), ({"verb": "pause"}, "cyan"),
    ({"verb": "goto", "target": "impl"}, "cyan"),
    ({"verb": "unknown"}, None),
    # bare strings still tolerated (legacy/fallback path)
    ("continue", "green"), ("retry", "yellow"), ("mystery", None),
])
def test_accent_color_for_action(action, expected) -> None:
    assert pal.accent_color(action) == expected


def test_action_display_text() -> None:
    assert pal.action_text({"verb": "continue"}) == "continue"
    assert pal.action_text({"verb": "goto", "target": "impl"}) == "goto(impl)"
    assert pal.action_text({"verb": "pause"}) == "pause"
    assert pal.action_text({"verb": "goto"}) == "goto"   # no target → bare verb
    assert pal.action_text("continue") == "continue"     # bare string fallback


def test_action_text_goto_resume_marker() -> None:
    assert pal.action_text(
        {"verb": "goto", "target": "coder", "resume": True}) == "goto(coder) [resume]"
    # resume false (or absent) renders the plain goto
    assert pal.action_text(
        {"verb": "goto", "target": "coder", "resume": False}) == "goto(coder)"
    assert pal.action_text({"verb": "goto", "target": "coder"}) == "goto(coder)"


def test_action_text_resume_keeps_goto_color() -> None:
    # the verb stays "goto", so accent coloring is unaffected by resume
    assert pal.accent_color({"verb": "goto", "target": "coder", "resume": True}) == "cyan"


def test_fold_glyphs_default_unicode() -> None:
    assert pal.fold_glyph(expanded=False) == "▸"
    assert pal.fold_glyph(expanded=True) == "▾"


def test_agent_color_present() -> None:
    assert pal.NODE_COLORS["agent"] == ("bold",)
