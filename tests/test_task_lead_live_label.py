from __future__ import annotations

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog


def _render_task_lead() -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["task-lead"].prompt_template_source
    ctx = build_context(change_title="t", event_log=InMemoryEventLog(),
                        change_id="chg", change_home=".imp/changes/chg",
                        vars={"fast_path": False, "plan_index": 2,
                              "plan_total": 4, "task_total": 6})
    return render_prompt(body, ctx, workflow_dir=bundled_workflow_dir("imp"))


def test_task_lead_instructs_the_live_label_push() -> None:
    out = _render_task_lead()
    # Plan prefix rendered from vars; <N> stays a placeholder for the task number.
    assert "implementing · plan 2/4 · task <N>/6" in out
    # bare case: plan_total=0 drops the denominator
    wf = load_workflow(bundled_workflow_dir("imp"))
    body = wf.agents["task-lead"].prompt_template_source
    ctx_bare = build_context(change_title="t", event_log=InMemoryEventLog(),
                             change_id="chg", change_home=".imp/changes/chg",
                             vars={"fast_path": False, "plan_index": 2,
                                   "plan_total": 0, "task_total": 6})
    out_bare = render_prompt(body, ctx_bare, workflow_dir=bundled_workflow_dir("imp"))
    assert "implementing · plan 2 · task <N>/6" in out_bare
