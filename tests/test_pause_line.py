from __future__ import annotations

from pathlib import Path

from imp.cli.main import _pause_lines, _phase_from_events
from tests._event_fixtures import ev


def test_pause_lines_named_change_renders_id_middot_title_plain() -> None:
    lines = _pause_lines("dufa26", "implement", "dufa26",
                         title="Add version flag", color=False)
    assert lines == [
        "dufa26 · Add version flag  at implement",
        "   resume:  imp resume dufa26",
    ]


def test_pause_lines_unnamed_change_renders_bare_id_no_middot() -> None:
    lines = _pause_lines("dufa26", "implement", "dufa26", color=False)
    assert lines[0] == "dufa26  at implement"
    assert " · " not in lines[0]


def test_pause_lines_without_a_phase_drop_the_at_clause() -> None:
    lines = _pause_lines("dufa26", None, "dufa26",
                         title="Add version flag", color=False)
    assert lines[0] == "dufa26 · Add version flag"


def test_pause_lines_reason_joins_with_a_middot_not_parens() -> None:
    lines = _pause_lines("dufa26", "implement", "dufa26",
                         title="Add version flag",
                         reason="internal_error: cannot access 'ev'",
                         color=False)
    assert lines[0] == (
        "dufa26 · Add version flag  at implement · "
        "internal_error: cannot access 'ev'")
    assert "(" not in lines[0]


def test_pause_lines_color_tints_headline_no_glyph_no_paused_word() -> None:
    lines = _pause_lines("dufa26", "implement", "dufa26",
                         title="Add version flag", color=True)
    assert lines[0] == "\x1b[33mdufa26 · Add version flag  at implement\x1b[0m"
    assert lines[1] == "\x1b[33m   resume:  imp resume dufa26\x1b[0m"
    assert "⏸" not in lines[0]
    assert "Paused" not in lines[0]


def test_pause_lines_color_with_reason_tints_whole_headline() -> None:
    lines = _pause_lines("dufa26", "implement", "dufa26",
                         title="Add version flag",
                         reason="internal_error: cannot access 'ev'",
                         color=True)
    assert lines[0] == (
        "\x1b[33mdufa26 · Add version flag  at implement · "
        "internal_error: cannot access 'ev'\x1b[0m")


def test_phase_from_events_takes_the_last_step_started_phase() -> None:
    phase = _phase_from_events([
        ev("1", "step_started", step="spec-writer", kind="agent", phase="spec"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="implement"),
        ev("3", "tool_call", agent_id="a", id="b", name="bash", input={}),
    ])
    assert phase == "implement"


def test_phase_from_events_none_when_no_step_started() -> None:
    assert _phase_from_events([ev("1", "change_created", quest_id=1, title="x")]) is None


def test_pause_facts_unnamed_change_shows_bare_id(tmp_path: Path) -> None:
    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    change = Change(id="qzbscf", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=tmp_path / ".imp" / "change")
    name, title, _phase, token, _reason = _pause_facts(change)
    assert name == "qzbscf"
    assert token == "qzbscf"
    assert title == ""


def test_pause_facts_named_change_shows_bare_id(tmp_path: Path) -> None:
    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    change = Change(id="qzbscf", title="Add version flag",
                    base_branch="main", control_root=tmp_path,
                    project_root=tmp_path,
                    quest_dir=tmp_path / ".imp" / "change")
    name, title, _phase, token, _reason = _pause_facts(change)
    assert name == "qzbscf"          # the bare id, not a slugified handle
    assert token == "qzbscf"
    assert title == "Add version flag"


def test_pause_reason_terminated_appends_first_line_of_last_agent_text() -> None:
    from imp.cli.main import _pause_reason

    reason = _pause_reason([
        ev("1", "step_started", step="plan-reviewer", kind="agent",
           phase="reviewing plan"),
        ev("2", "error", category="terminated",
           detail={"last_agent_text":
                   "API Error 400 — tool use concurrency\nstack trace line"}),
        ev("3", "workflow_paused", reason="terminated", kind="terminated"),
    ])
    assert reason == "terminated: API Error 400 — tool use concurrency"


def test_pause_reason_interrupt_has_no_cause() -> None:
    from imp.cli.main import _pause_reason

    # interrupt_reason wins over reason (mirrors _h_workflow_paused); distinct
    # values make that precedence falsifiable.
    reason = _pause_reason([
        ev("1", "workflow_paused", reason="terminated", interrupt_reason="interrupt"),
    ])
    assert reason == "interrupt"


def test_pause_reason_interrupt_after_prior_terminated_pause_has_no_stale_cause() -> None:
    from imp.cli.main import _pause_reason

    # Cumulative log across resume: a terminated pause, then a fresh invocation
    # that the user ctrl-c'd. The interrupt pause's own segment has no error, so
    # the stale terminated cause must NOT leak onto the interrupt headline.
    reason = _pause_reason([
        ev("1", "error", category="terminated",
           detail={"last_agent_text": "API Error 400 — tool use concurrency"}),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_paused", reason="interrupt", interrupt_reason="interrupt"),
    ])
    assert reason == "interrupt"


def test_pause_reason_none_when_no_pause_event() -> None:
    from imp.cli.main import _pause_reason

    assert _pause_reason([ev("1", "change_created", quest_id=1, title="x")]) is None


def test_pause_reason_internal_error_falls_back_to_message_first_line() -> None:
    from imp.cli.main import _pause_reason

    # An internal-error pause has no last_agent_text; the cause must come from
    # the error payload's message (first line only).
    reason = _pause_reason([
        ev("1", "error", category="internal", kind="PromptRenderError",
           message="project has no fact 'guidance'\n  at line 3 of template"),
        ev("2", "workflow_paused", reason="internal_error", kind="PromptRenderError"),
    ])
    assert reason == "internal_error: project has no fact 'guidance'"


def test_pause_reason_last_agent_text_wins_over_message() -> None:
    from imp.cli.main import _pause_reason

    # When both are present, detail.last_agent_text takes precedence.
    reason = _pause_reason([
        ev("1", "error", category="terminated",
           message="internal fallback message",
           detail={"last_agent_text": "API Error 400 — tool use concurrency"}),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
    ])
    assert reason == "terminated: API Error 400 — tool use concurrency"


def test_pause_reason_bare_reason_when_error_has_no_cause_fields() -> None:
    from imp.cli.main import _pause_reason

    # An error with neither last_agent_text nor message yields the bare reason.
    reason = _pause_reason([
        ev("1", "error", category="internal", kind="ValueError"),
        ev("2", "workflow_paused", reason="internal_error", kind="ValueError"),
    ])
    assert reason == "internal_error"


def test_pause_reason_resumed_after_paused_returns_stale_sentinel() -> None:
    from imp.cli.main import _pause_reason, _PauseBanner

    # A pause, then a resume with no new pause in the latest segment: the stored
    # reason is a fossil and must not be replayed as the headline.
    reason = _pause_reason([
        ev("1", "workflow_paused", reason="terminated", kind="terminated"),
        ev("2", "workflow_resumed"),
    ])
    assert reason is _PauseBanner.STALE


def test_pause_reason_paused_after_resumed_returns_real_reason() -> None:
    from imp.cli.main import _pause_reason

    # Pause -> resume -> pause again: the latest segment DID end with a pause,
    # so its real reason is the headline (not the stale sentinel).
    reason = _pause_reason([
        ev("1", "workflow_paused", reason="terminated", kind="terminated"),
        ev("2", "workflow_resumed"),
        ev("3", "workflow_paused", reason="interrupt", interrupt_reason="interrupt"),
    ])
    assert reason == "interrupt"


def test_pause_reason_resumed_before_any_pause_is_not_stale() -> None:
    from imp.cli.main import _pause_reason

    # A resume that precedes every pause must not mark a later pause stale.
    reason = _pause_reason([
        ev("1", "workflow_resumed"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
    ])
    assert reason == "terminated"


def test_pause_reason_resumed_with_no_prior_pause_is_stale() -> None:
    from imp.cli.main import _pause_reason, _PauseBanner

    # A parked draft has conversation_saved but never workflow_paused. Its
    # resume dying before any event (e.g. a workflow LoadError at daemon
    # startup) must still read as an unexpected death, not a mute banner.
    reason = _pause_reason([
        ev("1", "awaiting_user_input", prompt=""),
        ev("2", "conversation_saved"),
        ev("3", "workflow_resumed"),
    ])
    assert reason is _PauseBanner.STALE


def test_pause_reason_conversation_saved_after_resume_is_not_stale() -> None:
    from imp.cli.main import _pause_reason

    # A resumed draft that re-parks cleanly ends its segment with
    # conversation_saved — that is a clean end, not an unexpected death.
    reason = _pause_reason([
        ev("1", "conversation_saved"),
        ev("2", "workflow_resumed"),
        ev("3", "conversation_saved"),
    ])
    assert reason is None


def test_pause_facts_stale_with_crash_stderr_renders_unexpected_death_banner(
    tmp_path: Path,
) -> None:
    import json

    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    quest_dir = tmp_path / ".imp" / "changes" / "mona44"
    quest_dir.mkdir(parents=True)
    events = [
        ev("1", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_resumed"),
    ]
    (quest_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    # A genuine death: the daemon left a traceback in its stderr.
    (quest_dir / "daemon.stderr").write_text(
        "Traceback (most recent call last):\n  ...\nRuntimeError: boom\n")
    change = Change(id="mona44", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=quest_dir)
    name, _title, phase, token, reason = _pause_facts(change)
    assert name == "mona44"
    assert phase == "converge"
    assert token == "mona44"
    assert reason == "ended unexpectedly — see .imp/changes/mona44/daemon.stderr"


def test_pause_facts_stale_clean_detach_renders_calm_interrupt(
    tmp_path: Path,
) -> None:
    import json

    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    quest_dir = tmp_path / ".imp" / "changes" / "mona44"
    quest_dir.mkdir(parents=True)
    # Same resumed-without-pause STALE event shape as a crash...
    events = [
        ev("1", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_resumed"),
    ]
    (quest_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    # ...but daemon.stderr holds only benign INFO chatter — a clean detach.
    (quest_dir / "daemon.stderr").write_text("Using bundled Claude Code CLI\n")
    change = Change(id="mona44", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=quest_dir)
    _name, _title, _phase, _token, reason = _pause_facts(change)
    assert reason == "interrupt"
    assert "daemon.stderr" not in (reason or "")
    assert "ended unexpectedly" not in (reason or "")


def test_pause_facts_stale_empty_stderr_renders_calm_interrupt(tmp_path: Path) -> None:
    import json

    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    # daemon.stderr present but EMPTY (0 bytes): no crash traceback, so a clean
    # detach — the calm interrupt reason, no pointer (the empty-read path,
    # distinct from the missing-file OSError path).
    quest_dir = tmp_path / ".imp" / "changes" / "mona44"
    quest_dir.mkdir(parents=True)
    events = [
        ev("1", "step_started", step="task-lead", kind="agent", phase="converge"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_resumed"),
    ]
    (quest_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    (quest_dir / "daemon.stderr").write_bytes(b"")
    change = Change(id="mona44", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=quest_dir)
    *_, reason = _pause_facts(change)
    assert reason == "interrupt"
    assert "daemon.stderr" not in (reason or "")


def test_pause_facts_deliberate_leave_renders_trigger_over_stale(tmp_path: Path) -> None:
    import json

    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    # Event log that, WITHOUT a deliberate-leave signal, reads as STALE (a pause
    # followed by a resume) — the daemon-derived path would render the
    # unexpected-death banner. The stamped trigger must win instead.
    quest_dir = tmp_path / ".imp" / "changes" / "krol99"
    quest_dir.mkdir(parents=True)
    events = [
        ev("1", "step_started", step="task-lead", kind="agent", phase="implement"),
        ev("2", "workflow_paused", reason="terminated", kind="terminated"),
        ev("3", "workflow_resumed"),
    ]
    (quest_dir / "events.jsonl").write_text(
        "\n".join(json.dumps(e) for e in events) + "\n")
    change = Change(id="krol99", title="Leave now", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    quest_dir=quest_dir)

    name, title, phase, token, reason = _pause_facts(change, leave_trigger="sigterm")
    assert name == "krol99"
    assert title == "Leave now"          # title carried on the deliberate-leave path
    assert phase == "implement"          # last-seen phase, local knowledge
    assert token == "krol99"
    assert reason == "sigterm"           # the client's trigger, NOT "ended unexpectedly"


def test_leave_trigger_returns_reason_only_when_escalated_and_stamped() -> None:
    from imp.cli.main import _leave_trigger

    class _Evt:
        def __init__(self, is_set: bool) -> None:
            self._set = is_set

        def is_set(self) -> bool:
            return self._set

    class _Coord:
        def __init__(self, reason: str | None, evt: object | None) -> None:
            self.leave_reason = reason
            self.escalation_event = evt

    # A deliberate leave: escalation set AND a stamped trigger.
    assert _leave_trigger(_Coord("ctrl-d", _Evt(True))) == "ctrl-d"
    # Escalation set but no trigger stamped (a /save-style disposition leave).
    assert _leave_trigger(_Coord(None, _Evt(True))) is None
    # A trigger present but escalation not fired (defensive: not a completed leave).
    assert _leave_trigger(_Coord("sigterm", _Evt(False))) is None
    # No escalation event at all, and the None coord used by non-interactive paths.
    assert _leave_trigger(_Coord("sigterm", None)) is None
    assert _leave_trigger(None) is None


def test_daemon_stderr_has_crash_detects_traceback(tmp_path: Path) -> None:
    from imp.cli.main import _daemon_stderr_has_crash

    p = tmp_path / "daemon.stderr"
    p.write_text(
        "Traceback (most recent call last):\n"
        '  File "runner.py", line 10, in <module>\n'
        "RuntimeError: boom\n")
    assert _daemon_stderr_has_crash(p) is True


def test_daemon_stderr_has_crash_false_for_benign_info_only(tmp_path: Path) -> None:
    # A clean run leaves only benign provider INFO chatter — non-empty, no crash.
    from imp.cli.main import _daemon_stderr_has_crash

    p = tmp_path / "daemon.stderr"
    p.write_text("Using bundled Claude Code CLI\n")
    assert _daemon_stderr_has_crash(p) is False


def test_daemon_stderr_has_crash_false_when_file_absent(tmp_path: Path) -> None:
    from imp.cli.main import _daemon_stderr_has_crash

    assert _daemon_stderr_has_crash(tmp_path / "daemon.stderr") is False


def test_pause_reason_awaiting_user_has_no_reason() -> None:
    from imp.cli.main import _pause_reason

    # A genuine your-turn park surfaces no banner reason suffix: the trailing
    # line reads "Paused <name> at <phase>" with no "(...)".
    reason = _pause_reason([
        ev("1", "step_started", step="coder", kind="agent", phase="implement"),
        ev("2", "workflow_paused", reason="awaiting_user"),
    ])
    assert reason is None
