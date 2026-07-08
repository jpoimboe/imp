from __future__ import annotations

from unittest.mock import patch

from imp.cli import main
from imp.engine.executor import RunResult


class _Events:
    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def read(self) -> list[dict]:
        return list(self._events)


class _Change:
    id = "2026-06-12-run-x"
    title = ""
    base_branch = "master"
    state_dir = "/tmp/imp-test-state"   # never touched: State.load is patched

    def __init__(self, events: list[dict]) -> None:
        self._events = events

    def event_log(self) -> _Events:
        return _Events(self._events)


class _State:
    def __init__(self, status: str, step: str | None = None) -> None:
        self.status = status
        self.step = step


def test_merge_blocked_outcome_emits_no_paused_summary() -> None:
    change = _Change([{"type": "merge_blocked",
                       "payload": {"reason": "refused", "base": "master",
                                   "detail": "error: Aborting"}}])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_merge_blocked") as mb, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("merge_blocked")
        rc = main._handle_terminal_outcome(change, RunResult.PAUSED, coord=None)
    assert rc == 1
    mb.assert_called_once()          # the friendly ⚠ block still prints
    ps.assert_not_called()           # the redundant second pause line is gone


def test_non_merge_pause_still_emits_paused_summary() -> None:
    change = _Change([])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(change, RunResult.PAUSED, coord=None)
    assert rc == 1
    ps.assert_called_once()          # ordinary pauses keep the summary line


def test_emit_completed_merge_lines_render_lifecycle_wordmark(monkeypatch, capsys) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    for theme in ("ansi", "dark", "light"):
        pal.set_theme(theme)
        try:
            main._emit_completed(_Change([]), "shipped it", merged=True)
            out = capsys.readouterr().out
            assert "✓ Merged 2026-06-12-run-x into master." in out
            assert "Change 2026-06-12-run-x: shipped it" in out
            assert pal.sgr("wordmark", enabled=True) in out
            assert pal._ANSI["green"] not in out
        finally:
            pal.set_theme("ansi")


def test_emit_completed_non_merge_stays_green(monkeypatch, capsys) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    main._emit_completed(_Change([]), "all done", merged=False)
    out = capsys.readouterr().out
    assert "Change 2026-06-12-run-x: all done" in out
    assert pal._ANSI["green"] in out
    assert pal._ANSI["yellow"] not in out


def test_emit_merge_blocked_notice_is_wordmark_under_dark_theme(monkeypatch, capsys) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    try:
        pal.set_theme("dark")
        main._emit_merge_blocked(_Change([]), "worktree_dirty")
        out = capsys.readouterr().out
        assert pal.sgr("wordmark", enabled=True) in out   # \x1b[38;2;255;154;60m
        assert pal.sgr("yellow", enabled=True) not in out  # not the muted yellow role
    finally:
        pal.set_theme("ansi")


def test_emit_script_blocked_notice_is_wordmark_under_dark_theme(monkeypatch, capsys) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    try:
        pal.set_theme("dark")
        main._emit_script_blocked("integrate-rebase", "rebase conflict", "")
        out = capsys.readouterr().out
        assert pal.sgr("wordmark", enabled=True) in out
        assert pal.sgr("yellow", enabled=True) not in out
    finally:
        pal.set_theme("ansi")


def test_emit_script_blocked_renders_combined_stderr_then_stdout(capsys) -> None:
    main._emit_script_blocked("verify", "the-stderr-part", "the-stdout-part")
    out = capsys.readouterr().out
    assert "the-stderr-part" in out
    assert "the-stdout-part" in out
    assert out.index("the-stderr-part") < out.index("the-stdout-part")


def test_emit_script_blocked_bounds_detail_to_tail(capsys) -> None:
    from imp.cli.render_text import _SCRIPT_DETAIL_TAIL_LINES
    stdout = "\n".join(f"line-{i}" for i in range(_SCRIPT_DETAIL_TAIL_LINES + 20))
    main._emit_script_blocked("verify", "", stdout)
    out = capsys.readouterr().out
    assert "line-0" not in out
    assert f"line-{_SCRIPT_DETAIL_TAIL_LINES + 19}" in out


def test_handle_terminal_outcome_passes_combined_output(capsys) -> None:
    events = [
        {"type": "script_blocked",
         "payload": {"step": "verify", "stderr": "err-detail", "stdout": "out-detail", "exit": 3}},
    ]
    change = _Change(events)
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_paused_summary"):
        State.load.return_value = _State("paused", step="verify")
        rc = main._handle_terminal_outcome(change, RunResult.PAUSED, coord=None)
    out = capsys.readouterr().out
    assert "err-detail" in out
    assert "out-detail" in out
    assert rc == 1


def test_merge_blocked_outcome_forwards_exhaustion_note() -> None:
    note = "imp attempted 3 recovery passes and the merge still failed."
    change = _Change([{"type": "merge_blocked",
                       "payload": {"reason": "conflict", "base": "master",
                                   "detail": "merge conflict", "note": note}}])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_merge_blocked") as mb, \
         patch.object(main, "_emit_paused_summary"):
        State.load.return_value = _State("merge_blocked")
        main._handle_terminal_outcome(change, RunResult.PAUSED, coord=None)
    mb.assert_called_once()
    assert mb.call_args.args[3] == note     # the note reached _emit_merge_blocked as the note arg


def test_emit_paused_summary_renders_terminated_reason(capsys) -> None:
    change = _Change([
        {"type": "step_started", "payload": {"phase": "reviewing plan"}},
        {"type": "error",
         "payload": {"detail": {"last_agent_text":
                                "API Error 400 — tool use concurrency\ntrace"}}},
        {"type": "workflow_paused",
         "payload": {"reason": "terminated", "kind": "terminated"}},
    ])
    main._emit_paused_summary(change)
    out = capsys.readouterr().out
    assert "· terminated: API Error 400 — tool use concurrency" in out
    assert "at reviewing plan" in out


def test_emit_paused_summary_interrupt_renders_bare_reason(capsys) -> None:
    change = _Change([
        {"type": "workflow_paused",
         "payload": {"reason": "interrupt", "interrupt_reason": "interrupt"}},
    ])
    main._emit_paused_summary(change)
    out = capsys.readouterr().out
    assert "· interrupt" in out


def test_emit_completed_appends_warning_count_suffix(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_Change([]), "all done", merged=False, warnings=2)
    out = capsys.readouterr().out
    assert "Change 2026-06-12-run-x: all done  (2 warnings)" in out


def test_emit_completed_warning_suffix_singular(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_Change([]), "all done", merged=False, warnings=1)
    out = capsys.readouterr().out
    assert "(1 warning)" in out
    assert "warnings" not in out


def test_emit_completed_no_suffix_when_zero_warnings(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_Change([]), "all done", merged=False, warnings=0)
    out = capsys.readouterr().out
    assert "all done" in out
    assert "warning" not in out


def test_emit_completed_appends_warning_suffix_on_merged_line(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_Change([]), "all done", merged=True, warnings=2)
    out = capsys.readouterr().out
    assert "Change 2026-06-12-run-x: all done  (2 warnings)" in out


def test_handle_terminal_outcome_threads_runner_warning_count(monkeypatch) -> None:
    events = [
        {"type": "runner_warning", "payload": {"message": "a"}},
        {"type": "runner_warning", "payload": {"message": "a"}},
        {"type": "step_started", "payload": {"step": "s", "kind": "agent"}},
    ]
    change = _Change(events)
    change.control_root = main.Path("/tmp/imp-no-such-worktree-xyz")
    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, value, *, merged, warnings: captured.update(warnings=warnings))
    rc = main._handle_terminal_outcome(change, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["warnings"] == 2


def test_handle_terminal_outcome_threads_deliberate_leave_trigger() -> None:
    class _Evt:
        def is_set(self) -> bool:
            return True

    class _Coord:
        leave_reason = "ctrl-d"
        escalation_event = _Evt()

    change = _Change([])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(change, RunResult.PAUSED, coord=_Coord())
    assert rc == 1
    ps.assert_called_once()
    assert ps.call_args.args[1] == "ctrl-d"   # the trigger reached the summary


def test_handle_terminal_outcome_counts_warnings_from_archive_when_home_reaped(monkeypatch) -> None:
    # _Change([]) has no events_path attribute and event_log().read() returns [] —
    # so the code falls through to the archive_read.read_events fallback branch.
    change = _Change([])
    change.control_root = main.Path("/tmp/imp-no-such-worktree-xyz")

    # Two runner_warning events plus one non-warning event in the archived blob.
    archive_blob = (
        b'{"type": "runner_warning", "payload": {"message": "a"}}\n'
        b'{"type": "runner_warning", "payload": {"message": "b"}}\n'
        b'{"type": "step_started", "payload": {"step": "s"}}\n'
    )
    monkeypatch.setattr(
        main.archive_read, "read_events",
        lambda control_root, change_id: archive_blob)

    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, value, *, merged, warnings: captured.update(warnings=warnings))

    rc = main._handle_terminal_outcome(change, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["warnings"] == 2
