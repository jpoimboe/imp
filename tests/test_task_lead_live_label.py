from __future__ import annotations

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog


def _render_task_lead(**var_overrides) -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["task-lead"].prompt_template_source
    vars = {"fast_path": False, "plan_index": 2, "plan_total": 4, "task_total": 6,
            **var_overrides}
    ctx = build_context(change_title="t", event_log=InMemoryEventLog(),
                        change_id="chg", change_home=".imp/changes/chg",
                        vars=vars)
    return render_prompt(body, ctx, workflow_dir=bundled_workflow_dir("imp"))


def test_task_lead_instructs_the_three_live_label_verbs() -> None:
    out = _render_task_lead()
    assert "task <N>/6 · thinking" in out      # at task pick
    assert "task <N>/6 · coding" in out         # at coder dispatch
    assert "task <N>/6 · reviewing" in out      # before the reviewer


def test_task_lead_live_label_drops_the_phase_prefix() -> None:
    # The code-phase label now supplies `implementation phase · plan i/N`; the pushed
    # live label carries only the trailing `task …` segments.
    out = _render_task_lead()
    assert "code · plan" not in out
