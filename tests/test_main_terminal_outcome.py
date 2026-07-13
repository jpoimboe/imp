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
    quest = _Change([{"type": "merge_blocked",
                       "payload": {"reason": "refused", "base": "master",
                                   "detail": "error: Aborting"}}])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_merge_blocked") as mb, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("merge_blocked")
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    assert rc == 1
    mb.assert_called_once()          # the friendly ⚠ block still prints
    ps.assert_not_called()           # the redundant second pause line is gone


def test_non_merge_pause_still_emits_paused_summary() -> None:
    quest = _Change([])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    assert rc == 1
    ps.assert_called_once()          # ordinary pauses keep the summary line


def test_emit_merge_blocked_headline_red_explanation_wordmark(monkeypatch, capsys) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    try:
        pal.set_theme("dark")
        main._emit_merge_blocked(_Change([]), "worktree_dirty")
        out = capsys.readouterr().out
        assert pal.sgr("red", enabled=True) in out       # red headline tail
        assert pal.sgr("wordmark", enabled=True) in out  # gold explanation body
        assert "⚠" not in out
    finally:
        pal.set_theme("ansi")



def test_handle_terminal_outcome_does_not_reprint_script_detail(capsys) -> None:
    events = [
        {"type": "script_blocked",
         "payload": {"step": "verify", "stderr": "err-detail", "stdout": "out-detail", "exit": 3}},
    ]
    quest = _Change(events)
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step="verify")
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    out = capsys.readouterr().out
    assert "err-detail" not in out       # the exit recap no longer reprints script detail
    assert "out-detail" not in out
    ps.assert_called_once()              # only the paused summary is emitted
    assert rc == 1


def test_merge_blocked_outcome_forwards_exhaustion_note() -> None:
    note = "imp attempted 3 recovery passes and the merge still failed."
    quest = _Change([{"type": "merge_blocked",
                       "payload": {"reason": "conflict", "base": "master",
                                   "detail": "merge conflict", "note": note}}])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_merge_blocked") as mb, \
         patch.object(main, "_emit_paused_summary"):
        State.load.return_value = _State("merge_blocked")
        main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=None)
    mb.assert_called_once()
    assert mb.call_args.args[3] == note     # the note reached _emit_merge_blocked as the note arg


def test_emit_paused_summary_renders_terminated_reason_red_no_at_locator(capsys) -> None:
    quest = _Change([
        {"type": "step_started", "payload": {"phase": "reviewing plan"}},
        {"type": "error",
         "payload": {"detail": {"last_agent_text":
                                "API Error 400 — tool use concurrency\ntrace"}}},
        {"type": "workflow_paused",
         "payload": {"reason": "terminated", "kind": "terminated"}},
    ])
    main._emit_paused_summary(quest)
    out = capsys.readouterr().out
    assert out.startswith("\n")                        # one leading blank
    assert not out.startswith("\n\n")                  # exactly one
    assert "terminated: API Error 400 — tool use concurrency" in out
    assert " at reviewing plan" not in out             # locator dropped
    assert "resume: imp resume 2026-06-12-run-x" in out


def test_emit_paused_summary_interrupt_renders_bare_reason(capsys) -> None:
    quest = _Change([
        {"type": "workflow_paused",
         "payload": {"reason": "interrupt", "interrupt_reason": "interrupt"}},
    ])
    main._emit_paused_summary(quest)
    out = capsys.readouterr().out
    assert out.startswith("\n")                        # one leading blank
    assert "2026-06-12-run-x · interrupt" in out


def test_status_spine_named_renders_plain_id_title_tail_structure() -> None:
    line = main._status_spine("vahi04", "One-line title", "completed", "green",
                              color=False)
    assert line == "vahi04 · One-line title · completed"


def test_status_spine_unnamed_drops_the_title_segment() -> None:
    line = main._status_spine("vahi04", "", "completed", "green", color=False)
    assert line == "vahi04 · completed"
    # exactly one separator: id · tail, no empty title slot
    assert line.count(" · ") == 1


def test_status_spine_colors_id_gold_and_tail_by_role() -> None:
    from imp.cli import palette as pal
    line = main._status_spine("vahi04", "T", "completed", "green", color=True)
    assert line == (f'{pal.ansi("vahi04", "wordmark")} · T · '
                    f'{pal.ansi("completed", "green")}')


def test_emit_completed_merged_prints_single_green_tail_no_glyph(
    monkeypatch, capsys,
) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    main._emit_completed(_Change([]), merged=True)
    out = capsys.readouterr().out
    assert out.startswith("\n")                 # exactly one leading blank
    assert not out.startswith("\n\n")
    assert "2026-06-12-run-x" in out
    assert pal.ansi("merged into master", "green") in out
    assert "✓" not in out
    assert "quest 2026-06-12-run-x:" not in out  # the old second line is gone
    assert "Merged 2026-06-12-run-x into" not in out


def test_emit_completed_non_merge_tail_is_completed_green(monkeypatch, capsys) -> None:
    from imp.cli import palette as pal
    monkeypatch.setattr(main, "should_use_color", lambda stream: True)
    main._emit_completed(_Change([]), merged=False)
    out = capsys.readouterr().out
    # Target the TAIL, not a raw escape scan of the whole line: under the autouse
    # `ansi` theme the gold id (wordmark) and yellow share `\x1b[33m`, so a
    # line-wide `yellow not in out` check would fail on the legitimately-gold id.
    assert pal.ansi("completed", "green") in out
    assert pal.ansi("completed", "wordmark") not in out   # tail is green, not gold


def test_emit_completed_warnings_render_plain_deduped_list(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(
        _Change([]), merged=True,
        warnings=["clamped to 4", "clamped to 4", "id reused"])
    out = capsys.readouterr().out
    assert "3 warnings this run" in out
    assert "- clamped to 4 (×2)" in out
    assert "- id reused" in out
    assert "(3 warnings)" not in out            # the old count suffix is gone
    assert out.index("- clamped to 4 (×2)") < out.index("- id reused")  # first-seen order


def test_emit_completed_single_warning_header_is_singular(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_Change([]), merged=False, warnings=["only one"])
    out = capsys.readouterr().out
    assert "1 warning this run" in out
    assert "warnings this run" not in out
    assert "- only one" in out
    assert "(×" not in out                       # no count suffix at a single occurrence


def test_emit_completed_no_warnings_prints_no_block(monkeypatch, capsys) -> None:
    monkeypatch.setattr(main, "should_use_color", lambda stream: False)
    main._emit_completed(_Change([]), merged=False, warnings=[])
    out = capsys.readouterr().out
    assert "warning" not in out
    assert "this run" not in out


def test_handle_terminal_outcome_threads_runner_warning_messages(monkeypatch) -> None:
    events = [
        {"type": "runner_warning", "payload": {"message": "a"}},
        {"type": "runner_warning", "payload": {"message": "a"}},
        {"type": "step_started", "payload": {"step": "s", "kind": "agent"}},
    ]
    quest = _Change(events)
    quest.control_root = main.Path("/tmp/imp-no-such-worktree-xyz")
    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, *, merged, warnings: captured.update(warnings=warnings))
    rc = main._handle_terminal_outcome(quest, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["warnings"] == ["a", "a"]    # ordered messages, not a count


def test_handle_terminal_outcome_threads_deliberate_leave_trigger() -> None:
    class _Evt:
        def is_set(self) -> bool:
            return True

    class _Coord:
        leave_reason = "ctrl-d"
        escalation_event = _Evt()

    quest = _Change([])
    with patch.object(main, "State") as State, \
         patch.object(main, "_emit_paused_summary") as ps:
        State.load.return_value = _State("paused", step=None)
        rc = main._handle_terminal_outcome(quest, RunResult.PAUSED, coord=_Coord())
    assert rc == 1
    ps.assert_called_once()
    assert ps.call_args.args[1] == "ctrl-d"   # the trigger reached the summary


def test_handle_terminal_outcome_reads_warning_messages_from_archive(monkeypatch) -> None:
    quest = _Change([])
    quest.control_root = main.Path("/tmp/imp-no-such-worktree-xyz")
    archive_blob = (
        b'{"type": "runner_warning", "payload": {"message": "a"}}\n'
        b'{"type": "runner_warning", "payload": {"message": "b"}}\n'
        b'{"type": "step_started", "payload": {"step": "s"}}\n'
    )
    monkeypatch.setattr(
        main.archive_read, "read_events",
        lambda control_root, quest_id: archive_blob)
    captured: dict = {}
    monkeypatch.setattr(
        main, "_emit_completed",
        lambda ch, *, merged, warnings: captured.update(warnings=warnings))
    rc = main._handle_terminal_outcome(quest, RunResult.COMPLETED, coord=None)
    assert rc == 0
    assert captured["warnings"] == ["a", "b"]
