"""Tests for the imp log renderer (src/imp/cli/render.py)."""

from __future__ import annotations

import io

import pytest

from imp.cli import render


def test_style_disabled_returns_plain_text() -> None:
    assert render._style("hi", "red", enabled=False) == "hi"


def test_style_enabled_wraps_in_ansi() -> None:
    out = render._style("hi", "red", enabled=True)
    assert out == "\x1b[31mhi\x1b[0m"


def test_style_multiple_codes() -> None:
    out = render._style("hi", "bold", "magenta", enabled=True)
    # codes concatenated in order, single reset
    assert out == "\x1b[1m\x1b[35mhi\x1b[0m"


def test_should_use_color_off_when_not_a_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    stream = io.StringIO()  # not a tty
    assert render.should_use_color(stream) is False


def test_should_use_color_off_when_no_color_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    assert render.should_use_color(FakeTTY()) is False


def test_style_no_codes_returns_plain_text() -> None:
    assert render._style("hi", enabled=True) == "hi"


def test_should_use_color_on_when_tty_and_no_no_color(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)

    class FakeTTY:
        def isatty(self) -> bool:
            return True

    assert render.should_use_color(FakeTTY()) is True


def test_fmt_change_created() -> None:
    s = render._fmt_change_created(
        {"change_id": 42, "title": "add foo to bar"}, full=False, color=False,
    )
    assert s == "#42  add foo to bar"


def test_fmt_workflow_started() -> None:
    s = render._fmt_workflow_started(
        {"name": "standard-feature", "from_phase": "onboard"},
        full=False, color=False,
    )
    assert s == "standard-feature  (from onboard)"


def test_fmt_workflow_paused_with_reason() -> None:
    s = render._fmt_workflow_paused(
        {"reason": "ctrl-c"}, full=False, color=False,
    )
    assert s == "paused  (ctrl-c)"


def test_fmt_workflow_paused_no_reason() -> None:
    # Defensive: payload may be {} per executor.py line 100.
    s = render._fmt_workflow_paused({}, full=False, color=False)
    assert s == "paused"


def test_fmt_workflow_resumed() -> None:
    s = render._fmt_workflow_resumed({}, full=False, color=False)
    assert s == "resumed"


def test_fmt_phase_started() -> None:
    s = render._fmt_phase_started({"phase": "design"}, full=False, color=False)
    assert s == "design"


def test_fmt_phase_completed_no_color() -> None:
    s = render._fmt_phase_completed(
        {"phase": "design", "action": "next"}, full=False, color=False,
    )
    assert s == "design  → next"


def test_fmt_phase_completed_color_accents_action() -> None:
    s = render._fmt_phase_completed(
        {"phase": "design", "action": "loop"}, full=False, color=True,
    )
    # arrow dimmed, action green
    assert "\x1b[2m→\x1b[0m" in s
    assert "\x1b[32mloop\x1b[0m" in s


def test_fmt_phase_exhausted() -> None:
    s = render._fmt_phase_exhausted({"phase": "design"}, full=False, color=False)
    assert s == "design  → exhausted"


def test_fmt_phase_exhausted_color() -> None:
    s = render._fmt_phase_exhausted({"phase": "design"}, full=False, color=True)
    assert "\x1b[31mexhausted\x1b[0m" in s
