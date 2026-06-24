from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _build() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="code",
           model="m", provider="anthropic", label="task 1/5 · thinking",
           phase_label="implementation phase · plan 2/4"),
    ]:
        b.add(e)
    return b


def _steps(b: nv.NarrativeBuilder) -> list[nv.StepNode]:
    return [n for ph in b.tree.root.children if isinstance(ph, nv.PhaseNode)
            for n in ph.children if isinstance(n, nv.StepNode)]


def test_step_node_carries_phase_label() -> None:
    assert _steps(_build())[0].phase_label == "implementation phase · plan 2/4"


def test_phase_node_carries_label() -> None:
    phases = [ph for ph in _build().tree.root.children
              if isinstance(ph, nv.PhaseNode)]
    assert phases[0].label == "implementation phase · plan 2/4"


def test_current_activity_exposes_phase_label() -> None:
    assert _build().current_activity().phase_label == "implementation phase · plan 2/4"


def test_step_completed_copies_phase_label_to_breadcrumb() -> None:
    b = _build()
    b.add(ev("3", "step_completed", step="task-lead", kind="agent",
             action={"verb": "continue"}, var_mutations={}))
    crumbs = [c for c in _steps(b)[0].children if isinstance(c, nv.BreadcrumbLine)]
    assert crumbs and crumbs[0].phase_label == "implementation phase · plan 2/4"


def test_resume_reentry_resets_phase_label() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="code",
           model="m", provider="anthropic", label="task 1/5 · thinking",
           phase_label="implementation phase · plan 2/4"),
        ev("3", "workflow_paused", reason="ctrl-c"),
        ev("4", "workflow_resumed"),
        ev("5", "step_started", step="task-lead", kind="agent", phase="code",
           model="m", provider="anthropic", label="task 1/5 · thinking",
           phase_label="implementation phase · plan 3/4", resumed=True),
    ]:
        b.add(e)
    assert _steps(b)[0].phase_label == "implementation phase · plan 3/4"


def test_current_activity_burst_exposes_phase_label() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="code",
           model="m", provider="anthropic",
           phase_label="implementation phase · plan 2/4"),
        ev("3", "tool_call", agent_id="step:code/task-lead", id="b1",
           name="bash", input={"command": "make test"}),
    ]:
        b.add(e)
    assert b.current_activity().phase_label == "implementation phase · plan 2/4"


def test_current_activity_subagent_running_exposes_phase_label() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="code",
           model="m", provider="anthropic",
           phase_label="implementation phase · plan 2/4"),
        ev("3", "tool_call", agent_id="step:code/task-lead", id="t1",
           name="task", input={"subagent_name": "coder"}),
    ]:
        b.add(e)
    assert b.current_activity().phase_label == "implementation phase · plan 2/4"


def test_current_activity_chrome_step_exposes_phase_label() -> None:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="gate", kind="script", phase="verify",
           label="testing", command="make test",
           phase_label="implementation phase · plan 2/4"),
    ]:
        b.add(e)
    assert b.current_activity().phase_label == "implementation phase · plan 2/4"
