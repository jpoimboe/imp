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


def test_pause_lines_color_dims_both_lines_without_a_glyph() -> None:
    lines = _pause_lines("add-version-flag", "implement", "yaitd7", color=True)
    assert lines[0] == "\x1b[2mPaused  add-version-flag  at implement\x1b[0m"
    assert lines[1] == "\x1b[2m   resume:  imp resume yaitd7\x1b[0m"


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


def test_pause_lines_color_dims_with_reason() -> None:
    lines = _pause_lines("add-version-flag", "implement", "yaitd7", reason="ctrl-c", color=True)
    assert lines[0] == "\x1b[2mPaused  add-version-flag  at implement  (ctrl-c)\x1b[0m"


def test_pause_facts_unnamed_change_labels_with_resume_token(tmp_path: Path) -> None:
    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    change = Change(id="2026-06-14-qzbscf", title="", base_branch="main",
                    control_root=tmp_path, project_root=tmp_path,
                    change_dir=tmp_path / ".imp" / "change")
    name, _phase, token = _pause_facts(change)
    assert name == "qzbscf"
    assert token == "qzbscf"


def test_pause_facts_named_change_labels_with_slug_descriptor(tmp_path: Path) -> None:
    from imp.cli.main import _pause_facts
    from imp.engine.change import Change

    change = Change(id="2026-06-14-qzbscf", title="Add version flag",
                    base_branch="main", control_root=tmp_path,
                    project_root=tmp_path,
                    change_dir=tmp_path / ".imp" / "change")
    name, _phase, _token = _pause_facts(change)
    assert name == "add-version-flag"
