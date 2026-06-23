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


def test_task_lead_instructs_the_live_label_push() -> None:
    out = _render_task_lead()
    # Phase-first live label, with the trailing verb for each active subagent.
    assert "code · plan 2/4 · task <N>/6 · writing" in out
    assert "code · plan 2/4 · task <N>/6 · reviewing" in out


def test_task_lead_bare_case_drops_plan_denominator() -> None:
    out = _render_task_lead(plan_total=0)
    assert "code · plan 2 · task <N>/6 · writing" in out


def test_task_lead_bare_case_reviewing_drops_plan_denominator() -> None:
    out = _render_task_lead(plan_total=0)
    assert "code · plan 2 · task <N>/6 · reviewing" in out
