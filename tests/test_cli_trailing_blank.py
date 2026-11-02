from __future__ import annotations

import sys

import pytest

from imp.paths import project_state_dir


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


def test_show_ends_with_one_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    rc, out = _run_tty(monkeypatch, capsys, tmp_path, "show", "alpha", "--no-pager")
    assert rc == 0
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_log_ends_with_one_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.engine.quest import Quest
    from imp.engine.state import JsonlEventLog
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    # Add step events so the narrative tree has visible content to render.
    log = JsonlEventLog(project_state_dir(tmp_path) / "quests" / "alpha" / "events.jsonl")
    log.append(agent_id=None, type="step_started", payload={"step": "s"})
    log.append(agent_id=None, type="step_completed",
               payload={"step": "s", "status": "Success"})
    rc, out = _run_tty(monkeypatch, capsys, tmp_path, "log", "alpha", "--no-pager")
    assert rc == 0
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_search_hits_end_with_one_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    rc, out = _run_tty(monkeypatch, capsys, tmp_path, "search", "alpha", "--no-pager")
    assert rc == 0
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


def test_events_index_ends_with_one_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="A", quest_id="alpha", base_branch="main")
    rc, out = _run_tty(monkeypatch, capsys, tmp_path, "events", "alpha", "--no-pager")
    assert rc == 0
    assert out.endswith("\n\n")
    assert not out.endswith("\n\n\n")


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
    assert out.endswith("options.\n\n")   # last content line + one blank


def test_overview_unchanged_when_piped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setenv("NO_COLOR", "1")
    rc = m.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.endswith("options.\n")
    assert not out.endswith("options.\n\n")


def test_abandon_picker_empty_trailing_blank_on_tty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._abandon_picker_empty()
    assert rc == 0
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
    rc = m._cmd_list(argparse.Namespace(json=False))
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
    assert rc == 0
    assert capsys.readouterr().out == "Nothing to abandon.\n"


def test_cmd_config_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import io

    import imp.cli.config_cmd as cc
    import imp.cli.main as m
    import imp.providers.effective as eff_mod

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(
        eff_mod, "load_effective_config",
        lambda *, control_root: object())

    captured: dict = {}

    def fake_render(eff, out, *, state_dir, color, show_all):
        captured["out"] = out
        out.write("tiers:\n")
        out.write("  high opus\n")

    monkeypatch.setattr(cc, "render_config", fake_render)
    rc = m._cmd_config(argparse.Namespace(explain=None, all=False))
    assert rc == 0
    assert isinstance(captured["out"], io.StringIO)
    assert capsys.readouterr().out == "tiers:\n  high opus\n\n"


def test_cmd_config_unchanged_when_piped(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import io

    import imp.cli.config_cmd as cc
    import imp.cli.main as m
    import imp.providers.effective as eff_mod

    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(
        eff_mod, "load_effective_config",
        lambda *, control_root: object())

    captured: dict = {}

    def fake_render(eff, out, *, state_dir, color, show_all):
        captured["out"] = out
        out.write("tiers:\n")
        out.write("  high opus\n")

    monkeypatch.setattr(cc, "render_config", fake_render)
    rc = m._cmd_config(argparse.Namespace(explain=None, all=False))
    assert rc == 0
    assert isinstance(captured["out"], io.StringIO)
    assert capsys.readouterr().out == "tiers:\n  high opus\n"


def test_cmd_config_explain_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse
    import io

    import imp.cli.config_cmd as cc
    import imp.cli.main as m
    import imp.providers.effective as eff_mod

    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(
        eff_mod, "load_effective_config",
        lambda *, control_root: object())

    captured: dict = {}

    def fake_explain(eff, tier, out, *, color):
        captured["out"] = out
        out.write("explain high:\n")
        out.write("  opus\n")

    monkeypatch.setattr(cc, "render_explain", fake_explain)
    rc = m._cmd_config(argparse.Namespace(explain="high", all=False))
    assert rc == 0
    assert isinstance(captured["out"], io.StringIO)
    assert capsys.readouterr().out == "explain high:\n  opus\n\n"


def test_abandon_already_gone_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._abandon_local(tmp_path, "ghost")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "quest ghost already gone; ensured branch imp/ghost is removed.\n\n"


def test_record_idea_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import imp.cli.main as m
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._record_idea(
        argparse.Namespace(idea="Wire OAuth", opening=None, depends_on=None,
                           title=None, workflow=None),
        tmp_path, "main")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.endswith("`)\n\n")        # confirmation line + exactly one blank
    assert not out.endswith("\n\n\n")


def test_cmd_init_appends_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import imp.cli.main as m

    # Both streams a tty + non_interactive False => interactive branch (run_init).
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)

    def fake_run_init(project, *, input_fn, output_fn, **kwargs):
        output_fn("Created:")
        output_fn("  imp/config.yaml")

    monkeypatch.setattr("imp.cli.init.run_init", fake_run_init)
    rc = m._cmd_init(argparse.Namespace(non_interactive=False))
    assert rc == 0
    assert capsys.readouterr().out == "Created:\n  imp/config.yaml\n\n"


def test_cmd_init_unchanged_when_piped(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import imp.cli.main as m

    # Non-tty stdout auto-routes to the non-interactive writer; the auto-headless
    # note goes to stderr, so captured stdout stays blank-line-free when piped.
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(m, "_project_root", lambda: tmp_path)

    def fake_noninteractive(project, *, output_fn, **kwargs):
        output_fn("Created:")

    monkeypatch.setattr("imp.cli.init.run_init_noninteractive", fake_noninteractive)
    rc = m._cmd_init(argparse.Namespace(non_interactive=False))
    assert rc == 0
    assert capsys.readouterr().out == "Created:\n"


def test_abandon_already_gone_unchanged_when_piped(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    rc = m._abandon_local(tmp_path, "ghost")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "quest ghost already gone; ensured branch imp/ghost is removed.\n"


def test_record_idea_unchanged_when_piped(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import argparse

    import imp.cli.main as m
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    rc = m._record_idea(
        argparse.Namespace(idea="Wire OAuth", opening=None, depends_on=None,
                           title=None, workflow=None),
        tmp_path, "main")
    out = capsys.readouterr().out
    assert rc == 0
    assert out.endswith("`)\n")          # confirmation line + single newline
    assert not out.endswith("\n\n")      # no added blank when piped


def test_abandon_resident_delivery_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    monkeypatch.setattr("imp.remote.daemon.directory.session_is_resident", lambda _ctrl, cid: True)
    monkeypatch.setattr(m, "_deliver_abandon_resident", lambda cid: None)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._abandon_local(tmp_path, "feat")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "Abandoned feat · removed worktree and branch imp/feat (no commits).\n\n"


def test_abandon_tear_down_trailing_blank_on_tty(
    tmp_path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import imp.cli.main as m
    from imp.engine.quest import Quest
    from tests.conftest import _seed_control

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="x", quest_id="feat", base_branch="main")
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    rc = m._abandon_local(tmp_path, "feat")
    out = capsys.readouterr().out
    assert rc == 0
    assert out == "Abandoned feat · removed worktree and branch imp/feat (no commits).\n\n"


def test_conventions_doc_records_trailing_blank_rule() -> None:
    from pathlib import Path

    text = Path("docs/internals/conventions.md").read_text()
    assert "trailing blank line" in text
    assert "pipes" in text or "redirect" in text
    assert "machine" in text or "--json" in text
