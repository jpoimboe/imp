# tests/test_palette.py
from __future__ import annotations

import io

import pytest

from imp.cli import palette as pal


def test_ansi_disabled_returns_plain() -> None:
    assert pal.ansi("hi", "red", enabled=False) == "hi"


def test_ansi_enabled_wraps_and_resets_once() -> None:
    assert pal.ansi("hi", "bold", "magenta", enabled=True) == "\x1b[1m\x1b[35mhi\x1b[0m"


def test_should_use_color_requires_tty_and_no_NO_COLOR(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    assert pal.should_use_color(io.StringIO()) is False     # not a tty

    class TTY:
        def isatty(self) -> bool:
            return True
    assert pal.should_use_color(TTY()) is True
    monkeypatch.setenv("NO_COLOR", "1")
    assert pal.should_use_color(TTY()) is False


@pytest.mark.parametrize("action,expected", [
    ("continue", "green"), ("retry", "yellow"), ("pause", "cyan"),
    ({"kind": "goto", "target": "impl"}, "cyan"),
    ({"kind": "unknown"}, None), ("mystery", None),
])
def test_accent_color_for_action(action, expected) -> None:
    assert pal.accent_color(action) == expected


def test_action_display_text() -> None:
    assert pal.action_text("continue") == "continue"
    assert pal.action_text({"kind": "goto", "target": "impl"}) == "goto(impl)"
    assert pal.action_text({"kind": "pause"}) == "pause"


def test_fold_glyphs_default_unicode() -> None:
    assert pal.fold_glyph(expanded=False) == "▸"
    assert pal.fold_glyph(expanded=True) == "▾"


def test_agent_and_ask_user_colors_present() -> None:
    assert pal.NODE_COLORS["agent"] == ("bold",)
    assert pal.NODE_COLORS["ask_user"] == ("bold", "cyan")
