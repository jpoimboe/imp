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


def _run_tty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control,
    *argv: str,
) -> tuple[int, str]:
    import imp.cli.main as m

    monkeypatch.setattr(m, "_control_root", lambda: control)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    rc = m.main(list(argv))
    return rc, capsys.readouterr().out


def test_list_ends_with_one_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    rc, out = _run_tty(monkeypatch, capsys, tmp_path, "list", "--no-pager")
    assert rc == 0
    assert out.endswith("\n\n")          # exactly one trailing blank line
    assert not out.endswith("\n\n\n")    # never two or more


def test_show_json_has_no_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    rc, out = _run_tty(monkeypatch, capsys, tmp_path, "show", "alpha", "--json",
                       "--no-pager")
    assert rc == 0
    assert out.rstrip("\n") != ""        # there is real JSON output
    assert not out.endswith("\n\n")      # machine JSON stays blank-line-free


def test_list_has_no_trailing_blank_when_piped(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    from contextlib import redirect_stdout

    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()  # StringIO.isatty() is False -> piped semantics
    with redirect_stdout(buf):
        rc = m.main(["list", "--no-pager"])
    out = buf.getvalue()
    assert rc == 0
    assert not out.endswith("\n\n")      # piped output unchanged: no added blank
