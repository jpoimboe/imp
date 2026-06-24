from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev

NOTICE = "✓ plan 1/2 · 2 tasks\n   1. seed widget\n   2. wire widget"


def _stream() -> list[dict]:
    return [
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic", label="writing plan 1/2",
           phase_label="planning phase"),
        ev("3", "step_completed", step="plan-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("4", "step_started", step="plan-finalized", kind="agent", phase="plan",
           model="m", provider="anthropic", label="finalizing plan 1/2",
           phase_label="planning phase"),
        ev("5", "notice", agent_id="step:plan/plan-finalized", text=NOTICE),
        ev("6", "step_completed", step="plan-finalized", kind="agent",
           action={"verb": "goto", "target": "task-lead"}, var_mutations={"task_total": 2}),
        ev("7", "step_started", step="task-lead", kind="agent", phase="code",
           model="m", provider="anthropic", label="task 1/2 · thinking",
           phase_label="implementation phase · plan 1/2"),
        ev("8", "step_label_updated", agent_id="step:code/task-lead",
           label="task 1/2 · coding"),
        ev("9", "step_label_updated", agent_id="step:code/task-lead",
           label="task 2/2 · coding"),
        ev("10", "step_completed", step="task-lead", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
        ev("11", "step_started", step="task-lead", kind="agent", phase="code",
           model="m", provider="anthropic", label="task 1/3 · thinking",
           phase_label="implementation phase · plan 2/2"),
        ev("12", "step_label_updated", agent_id="step:code/task-lead",
           label="task 1/3 · coding"),
    ]


def _steps(root) -> list:
    return [st for ph in root.children if isinstance(ph, nv.PhaseNode)
            for st in ph.children if isinstance(st, nv.StepNode)]


def test_golden_status_bar_tracks_live_task_position() -> None:
    b = nv.NarrativeBuilder()
    stream = _stream()
    for e in stream[:8]:   # through the first live push (task 1/2 · coding)
        b.add(e)
    assert b.current_activity().step_label == "task 1/2 · coding"
    for e in stream[8:]:
        b.add(e)
    assert b.current_activity().step_label == "task 1/3 · coding"


def test_golden_breadcrumbs_show_frozen_label_not_live_position() -> None:
    b = nv.NarrativeBuilder()
    for e in _stream():
        b.add(e)
    crumbs = [c for st in _steps(b.tree.root) for c in st.children
              if isinstance(c, nv.BreadcrumbLine)]
    assert crumbs                                   # completed steps left breadcrumbs
    task_crumbs = [c for c in crumbs if (c.step_label or "").startswith("task ")]
    assert task_crumbs                              # the completed task-lead step
    for c in task_crumbs:
        assert c.step_label == "task 1/2 · thinking"     # frozen entry label
        assert "task 2/2" not in (c.step_label or "")    # never the live position


def test_golden_task_list_prints_once() -> None:
    b = nv.NarrativeBuilder()
    for e in _stream():
        b.add(e)
    literals = [c for st in _steps(b.tree.root) for c in st.children
                if isinstance(c, nv.LiteralTextNode)]
    assert sum(1 for n in literals if n.text == NOTICE and n.verbatim) == 1
