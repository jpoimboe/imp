from __future__ import annotations

import sys

import pytest


def test_with_trailing_blank_empty_input_yields_one_blank() -> None:
    from imp.cli.main import _with_trailing_blank

    assert _with_trailing_blank([]) == [""]


def test_with_trailing_blank_appends_one_when_missing() -> None:
    from imp.cli.main import _with_trailing_blank

    assert _with_trailing_blank(["alpha", "beta"]) == ["alpha", "beta", ""]


def test_with_trailing_blank_collapses_multiple_trailing_blanks() -> None:
    from imp.cli.main import _with_trailing_blank

    assert _with_trailing_blank(["alpha", "", "", ""]) == ["alpha", ""]


def test_with_trailing_blank_preserves_interior_blanks() -> None:
    from imp.cli.main import _with_trailing_blank

    assert _with_trailing_blank(["a", "", "b"]) == ["a", "", "b", ""]


def test_emit_human_adds_trailing_blank_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.cli.main import _emit_human

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    _emit_human(["alpha", "beta"])
    assert capsys.readouterr().out == "alpha\nbeta\n\n"


def test_emit_human_unchanged_when_not_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.cli.main import _emit_human

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    _emit_human(["alpha", "beta"])
    assert capsys.readouterr().out == "alpha\nbeta\n"
