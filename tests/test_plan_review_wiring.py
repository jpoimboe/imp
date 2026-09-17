"""The plan-review step runs a single plan-reviewer directly (no coordinator
panel): the step resolves to plan-reviewer.md at tier high, the engine routes
its {status, findings, critical, major, minor} output through two rules — a
blocking rework and a clean forward edge that hands the findings to task-lead
in plan_review_notes — and the dead plan-review-panel agent and plan_panel_done
var are gone."""
from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Goto

_BASE_VARS = {"fast_path": False, "plan_review_notes": ""}


def _wf():
    return load_workflow(bundled_workflow_dir("default"))


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


def test_plan_review_rules_route_forward_without_panel_var() -> None:
    rules = _steps(_wf())["plan-reviewer"].rules
    # critical -> rework to plan-writer; no panel var, no flag.
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"critical": 1, "major": 0, "minor": 0,
                               "findings": "f"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.action.resume is True
    assert m.var_mutations == {}
    # major-only also reworks: the `+ output.major` term is load-bearing
    major_only = evaluate_rules(rules, vars=_BASE_VARS,
                                output={"critical": 0, "major": 1, "minor": 0,
                                        "findings": "f"})
    assert isinstance(major_only.action, Goto)
    assert major_only.action.target == "plan-writer"
    assert major_only.action.resume is True
    assert major_only.var_mutations == {}

    # clean with minors -> forward, carrying the findings to the lead.
    m2 = evaluate_rules(rules, vars=_BASE_VARS,
                        output={"critical": 0, "major": 0, "minor": 2,
                                "findings": "Minor: tighten task 3"})
    assert isinstance(m2.action, Goto) and m2.action.target == "plan-finalized"
    assert m2.action.resume is False
    assert m2.var_mutations == {"plan_review_notes": "Minor: tighten task 3"}

    # clean, nothing actionable -> still forward, still carrying whatever the
    # reviewer wrote. plan-reviewer.md's finish examples tell it to say so
    # explicitly on this branch, so the carrier is this sentence, not "".
    m3 = evaluate_rules(
        rules, vars=_BASE_VARS,
        output={"critical": 0, "major": 0, "minor": 0,
                "findings": "No findings. Plan is complete and ready for implementation."})
    assert isinstance(m3.action, Goto) and m3.action.target == "plan-finalized"
    assert m3.action.resume is False
    assert m3.var_mutations == {
        "plan_review_notes":
            "No findings. Plan is complete and ready for implementation."}
