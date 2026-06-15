"""The plan-review step becomes a 2-member panel: agents/plan-review-panel.md
dispatches plan-reviewer subagents (2 on the first pass, 1 on bounce-back
re-verify keyed on plan_panel_done) and collapses OR-blocking / OR-minors /
verbatim-union, with no deferred-observations write. The step routes via the
existing minors-integration rules, now threading plan_panel_done."""
from __future__ import annotations

from pathlib import Path

from imp.agents.prompts import build_context, render_prompt
from imp.assets import default_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog
from imp.engine.types import Goto

AGENTS = Path("src/imp/assets/workflows/default/agents")

_BASE_VARS = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False,
    "plan_panel_done": False, "multi_plan": False, "review_scope": "plan",
}


def _wf():
    return load_workflow(default_workflow_dir())


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_plan_review_step_uses_panel_at_medium_tier() -> None:
    step = _steps(_wf())["plan-review"]
    assert step.kind == "agent" and step.tier == "medium"
    assert step.file.name == "plan-review-panel.md"


def test_plan_review_panel_declares_one_subagent_and_leaf_schema() -> None:
    panel = _wf().agents["plan-review-panel"]
    assert set(panel.subagents) == {"plan-reviewer"}
    names = set(panel.output_schema.field_names())
    assert {"status", "findings", "blocking", "has_minor_findings"} <= names


def _render(tmp_path, *, plan_panel_done: bool) -> str:
    wf = _wf()
    body = wf.agents["plan-review-panel"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(change_slug="t", change_title="t", event_log=log,
                        vars={"plan_panel_done": plan_panel_done})
    return render_prompt(body, ctx)


def test_first_pass_dispatches_two_high_reviewers(tmp_path) -> None:
    out = _render(tmp_path, plan_panel_done=False)
    assert out.count('task(subagent_name="plan-reviewer", tier="high"') == 2


def test_bounce_back_dispatches_one_high_reviewer(tmp_path) -> None:
    out = _render(tmp_path, plan_panel_done=True)
    assert out.count('task(subagent_name="plan-reviewer", tier="high"') == 1


def test_coordinator_omits_deferred_observations_write(tmp_path) -> None:
    out = _render(tmp_path, plan_panel_done=False)
    # review-panel.md writes this pinned line to progress.md; the plan coordinator
    # must NOT — there is no plan-stage sweep.
    assert "- [ ] DEFERRED" not in out


def test_coordinator_states_union_collapse(tmp_path) -> None:
    out = _render(tmp_path, plan_panel_done=False).lower()
    assert "verbatim union" in out
    assert "or of" in out                    # OR of members' blocking / minors
    assert "status: error" in out            # member Error treated as blocking


def test_plan_review_rules_thread_plan_panel_done() -> None:
    rules = _steps(_wf())["plan-review"].rules
    # blocking -> rework to plan-writer; dial set true for the bounce-back.
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"blocking": True, "has_minor_findings": False})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.action.resume is True
    assert m.var_mutations["plan_minors_integrated"] is False
    assert m.var_mutations["plan_panel_done"] is True
    # verification pass (minors already integrated) -> task-lead; dial reset false.
    m2 = evaluate_rules(rules, vars={**_BASE_VARS, "plan_minors_integrated": True},
                        output={"blocking": False, "has_minor_findings": True})
    assert isinstance(m2.action, Goto) and m2.action.target == "task-lead"
    assert m2.var_mutations["plan_minors_integrated"] is False
    assert m2.var_mutations["plan_panel_done"] is False
    # clean-with-minors -> one integration pass; dial set true.
    m3 = evaluate_rules(rules, vars=_BASE_VARS,
                        output={"blocking": False, "has_minor_findings": True})
    assert isinstance(m3.action, Goto) and m3.action.target == "plan-writer"
    assert m3.var_mutations["plan_minors_integrated"] is True
    assert m3.var_mutations["plan_panel_done"] is True
    # clean, nothing actionable -> task-lead; dial reset false.
    m4 = evaluate_rules(rules, vars=_BASE_VARS,
                        output={"blocking": False, "has_minor_findings": False})
    assert isinstance(m4.action, Goto) and m4.action.target == "task-lead"
    assert m4.var_mutations["plan_panel_done"] is False
