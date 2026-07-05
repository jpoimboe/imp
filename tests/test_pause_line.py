from __future__ import annotations

from pathlib import Path

from imp.cli.main import _pause_lines, _phase_from_events
from tests._event_fixtures import ev


def test_pause_lines_name_phase_and_resume_command_plain() -> None:
    lines = _pause_lines("add-version-flag", "implement", "yaitd7", color=False)
    assert lines == [
        "Paused  add-version-flag  at implement",
        "   resume:  imp resume yaitd7",
    ]


def test_pause_lines_without_a_phase_drop_the_at_clause() -> None:
    lines = _pause_lines("add-version-flag", None, "yaitd7", color=False)
    assert lines[0] == "Paused  add-version-flag"


def test_pause_lines_color_lifecycle_yellow_with_glyph_on_headline() -> None:
    lines = _pause_lines("add-version-flag", "implement", "yaitd7", color=True)
    assert lines[0] == "\x1b[33m⏸ Paused  add-version-flag  at implement\x1b[0m"
    assert lines[1] == "\x1b[33m   resume:  imp resume yaitd7\x1b[0m"
    assert "⏸" not in lines[1]                 # glyph leads only the headline


def test_pause_lines_color_lifecycle_yellow_with_reason() -> None:
    lines = _pause_lines("add-version-flag", "implement", "yaitd7",
                         reason="ctrl-c", color=True)
    assert lines[0] == "\x1b[33m⏸ Paused  add-version-flag  at implement  (ctrl-c)\x1b[0m"


def test_phase_from_events_takes_the_last_step_started_phase() -> None:
    phase = _phase_from_events([
        ev("1", "step_started", step="spec-writer", kind="agent", phase="spec"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="implement"),
        ev("3", "tool_call", agent_id="a", id="b", name="bash", input={}),
    ])
    assert phase == "implement"


def test_phase_from_events_none_when_no_step_started() -> None:
    assert _phase_from_events([ev("1", "change_created", change_id=1, title="x")]) is None


def test_pause_lines_include_reason_when_given() -> None:
    lines = _pause_lines("add-version-flag", "implement", "yaitd7",
                         reason="ctrl-c", color=False)
    assert lines[0] == "Paused  add-version-flag  at implement  (ctrl-c)"


def test_pause_facts_unnamed_change_shows_bare_id(tmp_path: Path) -> None:
    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    change = Change(id="qzbscf", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    change_dir=tmp_path / ".imp" / "change")
    name, _phase, token, _reason = _pause_facts(change)
    assert name == "qzbscf"
    assert token == "qzbscf"


def test_pause_facts_named_change_shows_bare_id(tmp_path: Path) -> None:
    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    change = Change(id="qzbscf", title="Add version flag",
                    base_branch="main", control_root=tmp_path,
                    project_root=tmp_path,
                    change_dir=tmp_path / ".imp" / "change")
    name, _phase, token, _reason = _pause_facts(change)
    assert name == "qzbscf"          # the bare id, not a slugified handle
    assert token == "qzbscf"


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

    assert _pause_reason([ev("1", "change_created", change_id=1, title="x")]) is None


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
