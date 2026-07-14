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


def test_status_ends_with_one_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    rc = m._cmd_status(argparse.Namespace(all=False), tmp_path)
    out = capsys.readouterr().out
    assert rc == 0
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_overview_ends_with_one_trailing_blank_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    rc = m.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.endswith("details.\n\n")   # overview's last content line + one blank


def test_overview_unchanged_when_piped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setenv("NO_COLOR", "1")
    rc = m.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.endswith("details.\n")
    assert not out.endswith("details.\n\n")


def test_show_picker_empty_trailing_blank_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._show_picker_empty()
    assert rc == 2
    assert capsys.readouterr().out == "No quests to show.\n\n"


def test_show_picker_empty_unchanged_when_piped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    rc = m._show_picker_empty()
    assert rc == 2
    assert capsys.readouterr().out == "No quests to show.\n"


def test_abandon_picker_empty_trailing_blank_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._abandon_picker_empty()
    assert rc == 2
    assert capsys.readouterr().out == "Nothing to abandon.\n\n"


def test_search_no_matches_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m
    from tests.conftest import _seed_control

    _seed_control(tmp_path)  # empty control: no quests, so search finds no hits
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    rc = m.main(["search", "nomatch"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "No quests match 'nomatch'.\n\n"   # spec-named early-exit, one blank


def test_events_not_started_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create_draft(tmp_path, title="Brief", quest_id="bk1",
                       base_branch="main", origin="idea", seed_opening="Brief")
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    rc = m.main(["events", "bk1"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "Quest bk1 hasn't started yet — no events to show.\n\n"


def test_cmd_list_no_quests_precondition_trailing_blank_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import imp.cli.main as m
    from imp.engine import worktree

    def _raise(): raise worktree.PreconditionError()
    monkeypatch.setattr(m, "_control_root", _raise)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._cmd_list(argparse.Namespace())
    assert rc == 0
    assert capsys.readouterr().out == m._NO_QUESTS_MESSAGE + "\n\n"


def test_log_picker_empty_trailing_blank_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._log_picker_empty()
    assert rc == 2
    assert capsys.readouterr().out == m._NO_QUESTS_MESSAGE + "\n\n"


def test_cmd_log_no_default_id_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import imp.cli.main as m

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = m._cmd_log(argparse.Namespace(show=None, hide=None, full=False,
                                       follow=False, token=None))
    assert rc == 0
    assert capsys.readouterr().out == m._NO_QUESTS_MESSAGE + "\n\n"


def test_cmd_events_no_default_id_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import imp.cli.main as m

    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    rc = m._cmd_events(argparse.Namespace(token=None))
    assert rc == 0
    assert capsys.readouterr().out == m._NO_QUESTS_MESSAGE + "\n\n"


def test_cmd_status_precondition_trailing_blank_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import imp.cli.main as m
    from imp.engine import worktree

    def _raise(): raise worktree.PreconditionError()
    monkeypatch.setattr(m, "_control_root", _raise)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._cmd_status(argparse.Namespace(all=False))
    assert rc == 0
    assert capsys.readouterr().out == m._NO_QUESTS_MESSAGE + "\n\n"


def test_abandon_picker_empty_unchanged_when_piped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    rc = m._abandon_picker_empty()
    assert rc == 2
    assert capsys.readouterr().out == "Nothing to abandon.\n"
