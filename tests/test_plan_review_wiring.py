"""The plan-review step runs a single plan-reviewer directly (no coordinator
panel): the step resolves to plan-reviewer.md at tier high, the engine routes
its {status, findings, blocking, has_minor_findings} output through the
unchanged minors-integration rules, and the dead plan-review-panel agent and
plan_panel_done var are gone."""
from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Goto

_BASE_VARS = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False, "review_scope": "plan",
}


def _wf():
    return load_workflow(bundled_workflow_dir("imp"))


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_plan_review_step_runs_single_reviewer_at_high_tier() -> None:
    step = _steps(_wf())["plan-reviewer"]
    assert step.kind == "agent" and step.tier == "high"
    assert step.file.name == "plan-reviewer.md"


def test_panel_agent_and_panel_var_are_gone() -> None:
    wf = _wf()
    assert "plan-review-panel" not in wf.agents
    assert "plan_panel_done" not in wf.vars.defaults()


def test_plan_review_rules_thread_minors_without_panel_var() -> None:
    rules = _steps(_wf())["plan-reviewer"].rules
    # blocking -> rework to plan-writer; clears the minors flag, no panel var.
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"blocking": True, "has_minor_findings": False})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.action.resume is True
    assert m.var_mutations == {"plan_minors_integrated": False}

    # verification pass (minors already integrated) -> task-lead; clears flag.
    m2 = evaluate_rules(rules, vars={**_BASE_VARS, "plan_minors_integrated": True},
                        output={"blocking": False, "has_minor_findings": True})
    assert isinstance(m2.action, Goto) and m2.action.target == "task-lead"
    assert m2.action.resume is False
    assert m2.var_mutations == {"plan_minors_integrated": False}

    # clean-with-minors -> one integration pass; sets the flag.
    m3 = evaluate_rules(rules, vars=_BASE_VARS,
                        output={"blocking": False, "has_minor_findings": True})
    assert isinstance(m3.action, Goto) and m3.action.target == "plan-writer"
    assert m3.action.resume is True
    assert m3.var_mutations == {"plan_minors_integrated": True}

    # clean, nothing actionable -> task-lead; no mutations.
    m4 = evaluate_rules(rules, vars=_BASE_VARS,
                        output={"blocking": False, "has_minor_findings": False})
    assert isinstance(m4.action, Goto) and m4.action.target == "task-lead"
    assert m4.action.resume is False
    assert m4.var_mutations == {}
