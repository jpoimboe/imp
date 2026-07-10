from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _build() -> nv.NarrativeBuilder:
    b = nv.NarrativeBuilder()
    for e in [
        ev("1", "change_created", quest_id=1, title="x"),
        ev("2", "step_started", step="task-lead", kind="agent", phase="code",
           model="m", provider="anthropic", label="task 1/6 · thinking",
           phase_label="implementation phase · plan 2/4", position_label=True),
    ]:
        b.add(e)
    return b


def test_live_label_takes_precedence_in_current_activity() -> None:
    b = _build()
    b.add(ev("3", "step_label_updated", agent_id="step:code/task-lead",
             label="task 3/6 · coding"))
    assert "task 3/6 · coding" in (b.current_activity().position or "")


def test_current_activity_falls_back_to_frozen_label() -> None:
    b = _build()
    assert "task 1/6 · thinking" in (b.current_activity().position or "")


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


def test_live_label_preserved_on_resume_re_emit_continuation() -> None:
    # Branch 1 (on-stack re-emit, no workflow_resumed window open): a resumed
    # step_started WITHOUT `restarted` is a plain pause/resume continuation and
    # must keep the pushed `task 3/6` live label, not revert to frozen `task 1/6`.
    b = _build()
    b.add(ev("3", "step_label_updated", agent_id="step:code/task-lead",
             label="task 3/6 · coding"))
    assert "task 3/6 · coding" in (b.current_activity().position or "")
    b.add(ev("4", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 1/6 · thinking",
             phase_label="implementation phase · plan 2/4", resumed=True,
             position_label=True))
    assert "task 3/6 · coding" in (b.current_activity().position or "")


def test_live_label_preserved_on_genuine_resume_continuation() -> None:
    # Branch 2 (workflow_resumed window open): the real resume path. A
    # continuation (`restarted` absent/False) preserves the pushed counter.
    b = _build()
    b.add(ev("3", "step_label_updated", agent_id="step:code/task-lead",
             label="task 3/6 · coding"))
    b.add(ev("4", "workflow_resumed"))
    b.add(ev("5", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 1/6 · thinking",
             phase_label="implementation phase · plan 2/4", resumed=True,
             restarted=False, position_label=True))
    assert "task 3/6 · coding" in (b.current_activity().position or "")


def test_live_label_cleared_on_genuine_resume_restart() -> None:
    # Branch 2 restart: a genuine re-run from scratch (`restarted` True) still
    # wipes the stale pushed label and falls back to the frozen static label.
    b = _build()
    b.add(ev("3", "step_label_updated", agent_id="step:code/task-lead",
             label="task 3/6 · coding"))
    b.add(ev("4", "workflow_resumed"))
    b.add(ev("5", "step_started", step="task-lead", kind="agent", phase="code",
             model="m", provider="anthropic", label="task 1/6 · thinking",
             phase_label="implementation phase · plan 2/4", resumed=True,
             restarted=True, position_label=True))
    pos = b.current_activity().position or ""
    assert "task 1/6 · thinking" in pos
    assert "task 3/6" not in pos
