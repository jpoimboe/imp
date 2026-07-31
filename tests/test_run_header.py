from __future__ import annotations

from imp.cli.main import _print_run_header, _run_header_line
from imp.paths import project_state_dir


def test_named_change_leads_with_id_then_title_and_marker() -> None:
    line = _run_header_line("yaitd7", "Add version flag", "new", color=False)
    assert line == "yaitd7 · Add version flag · new"


def test_unnamed_change_shows_id_and_marker_only() -> None:
    line = _run_header_line("yaitd7", "", "resuming", color=False)
    assert line == "yaitd7 · resuming"


def test_whitespace_only_title_shows_id_and_marker_only() -> None:
    line = _run_header_line("yaitd7", "   ", "new", color=False)
    assert line == "yaitd7 · new"


def test_color_wraps_head_gold_and_tail_dim() -> None:
    line = _run_header_line("yaitd7", "Add version flag", "new", color=True)
    assert line == (
        "\x1b[33myaitd7\x1b[0m"
        "\x1b[2m · Add version flag · new\x1b[0m")


def test_color_unnamed_shows_id_gold_and_dim_marker_tail() -> None:
    line = _run_header_line("yaitd7", "", "resuming", color=True)
    assert line == (
        "\x1b[33myaitd7\x1b[0m"
        "\x1b[2m · resuming\x1b[0m")


def test_print_run_header_emits_marker_then_one_blank_line(capsys) -> None:
    _print_run_header("b1f6wi", "", "chat", color=False)
    assert capsys.readouterr().out == "b1f6wi · chat\n\n"


def test_print_run_header_keeps_title_when_named(capsys) -> None:
    _print_run_header("b1f6wi", "Dark mode", "new", color=False)
    assert capsys.readouterr().out == "b1f6wi · Dark mode · new\n\n"


def test_marker_omitted_renders_id_and_title_only() -> None:
    line = _run_header_line("yaitd7", "Add version flag", color=False)
    assert line == "yaitd7 · Add version flag"


def test_marker_omitted_titleless_renders_id_only() -> None:
    line = _run_header_line("yaitd7", "   ", color=False)
    assert line == "yaitd7"


def test_marker_omitted_color_wraps_id_gold_and_title_dim() -> None:
    line = _run_header_line("yaitd7", "Add version flag", color=True)
    assert line == "\x1b[33myaitd7\x1b[0m\x1b[2m · Add version flag\x1b[0m"


def test_marker_omitted_titleless_color_is_id_only() -> None:
    line = _run_header_line("yaitd7", "", color=True)
    assert line == "\x1b[33myaitd7\x1b[0m"


def test_marker_omitted_titleless_whitespace_color_is_id_only() -> None:
    line = _run_header_line("yaitd7", "   ", color=True)
    assert line == "\x1b[33myaitd7\x1b[0m"


def _seed_log_quest(control) -> None:
    from imp.engine.quest import Quest
    from imp.engine.state import JsonlEventLog
    from tests.conftest import _seed_control

    _seed_control(control)
    Quest.create(control, title="A", quest_id="alpha", base_branch="main")
    log = JsonlEventLog(project_state_dir(control) / "quests" / "alpha" / "events.jsonl")
    log.append(agent_id=None, type="step_started", payload={"step": "s"})
    log.append(agent_id=None, type="step_completed",
               payload={"step": "s", "status": "Success"})


def test_log_snapshot_opens_with_id_title_header_on_tty(
    tmp_path, monkeypatch, capsys,
) -> None:
    import sys

    import imp.cli.main as m

    _seed_log_quest(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setenv("NO_COLOR", "1")
    rc = m.main(["log", "alpha", "--no-pager"])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("alpha · A\n\n")


def test_log_snapshot_header_present_when_piped(tmp_path, monkeypatch) -> None:
    import io
    from contextlib import redirect_stdout

    import imp.cli.main as m

    _seed_log_quest(tmp_path)
    monkeypatch.setattr(m, "_control_root", lambda: tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    buf = io.StringIO()  # StringIO.isatty() is False -> piped semantics
    with redirect_stdout(buf):
        rc = m.main(["log", "alpha", "--no-pager"])
    out = buf.getvalue()
    assert rc == 0
    assert out.startswith("alpha · A\n\n")
