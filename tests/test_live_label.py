from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _build() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="code",
           model="m", provider="anthropic", label="task 1/6 · thinking",
           phase_label="implementation phase · plan 2/4"),
    ]:
        b.add(e)
    return b


def test_live_label_takes_precedence_in_current_activity() -> None:
    b = _build()
    b.add(ev("3", "step_label_updated", agent_id="step:code/task-lead",
             label="task 3/6 · coding"))
    assert b.current_activity().step_label == "task 3/6 · coding"


def test_current_activity_falls_back_to_frozen_label() -> None:
    b = _build()
    assert b.current_activity().step_label == "task 1/6 · thinking"


def test_breadcrumb_reads_frozen_label_not_live_label() -> None:
    b = _build()
    b.add(ev("3", "step_label_updated", agent_id="step:code/task-lead",
             label="task 3/6 · coding"))
    b.add(ev("4", "step_completed", step="task-lead", kind="agent",
             action={"verb": "continue"}, var_mutations={}))
    root = b.tree.root
    steps = [n for ph in root.children if isinstance(ph, nv.PhaseNode)
             for n in ph.children if isinstance(n, nv.StepNode)]
    crumbs = [c for c in steps[0].children if isinstance(c, nv.BreadcrumbLine)]
    assert crumbs
    assert crumbs[0].step_label == "task 1/6 · thinking"
    assert "task 3/6" not in (crumbs[0].step_label or "")


def test_live_label_cleared_on_resume() -> None:
    b = _build()
    # Push a live label
    b.add(ev("3", "step_label_updated", agent_id="step:code/task-lead",
             label="task 3/6 · coding"))
    assert b.current_activity().step_label == "task 3/6 · coding"
    # Resume the same step — live_label must reset to the frozen label
    b.add(ev("4", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 1/6 · thinking",
             phase_label="implementation phase · plan 2/4", resumed=True))
    assert b.current_activity().step_label == "task 1/6 · thinking"
