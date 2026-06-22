from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Goto

_BASE = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False, "review_scope": "plan",
    "review_base": "",
    "change_panel_done": False, "plan_index": 3, "plan_total": 3, "task_total": 0,
}


def _steps():
    wf = load_workflow(bundled_workflow_dir("imp"))
    return {s.name: s for s in wf.flat_steps()}


def test_per_batch_settle_advances_plan_index() -> None:
    rules = _steps()["review-base-repin"].rules
    m = evaluate_rules(rules, vars={**_BASE, "review_scope": "plan"},
                       output={"exit": 0, "stdout": "cafef00d", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.var_mutations["plan_index"] == 4
    assert m.var_mutations["review_base"] == "cafef00d"


def test_end_of_change_settle_does_not_advance_plan_index() -> None:
    rules = _steps()["review-base-repin"].rules
    m = evaluate_rules(rules, vars={**_BASE, "review_scope": "change"},
                       output={"exit": 0, "stdout": "deadbeef", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "finalize"
    assert "plan_index" not in m.var_mutations


def test_finalize_rework_bumps_both_counters_together() -> None:
    rules = _steps()["finalize"].rules
    base = {"status": "Success", "built": "", "verify_status": "",
            "open_items": "", "next_steps": "", "comments": ""}
    m = evaluate_rules(rules, vars=_BASE,
                       output={**base, "decision": "rework", "comments": "fix X"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.var_mutations["plan_index"] == 4
    assert m.var_mutations["plan_total"] == 4


def test_finalize_merge_leaves_counters_alone() -> None:
    rules = _steps()["finalize"].rules
    m = evaluate_rules(rules, vars=_BASE,
                       output={"status": "Success", "decision": "merge", "comments": "",
                               "built": "", "verify_status": "", "open_items": "",
                               "next_steps": ""})
    assert isinstance(m.action, Goto) and m.action.target == "wait-for-head"
    assert "plan_index" not in m.var_mutations
    assert "plan_total" not in m.var_mutations


def test_resume_reworks_do_not_touch_plan_index() -> None:
    steps = _steps()
    pr = evaluate_rules(steps["plan-reviewer"].rules, vars=_BASE,
                        output={"blocking": True, "has_minor_findings": False})
    assert pr.action.target == "plan-writer" and pr.action.resume is True
    assert "plan_index" not in pr.var_mutations
    pv = evaluate_rules(steps["plan-verify-commit"].rules, vars=_BASE,
                        output={"exit": 1, "stdout": "", "stderr": ""})
    assert pv.action.target == "plan-writer" and pv.action.resume is True
    assert "plan_index" not in pv.var_mutations
    pr_minor = evaluate_rules(steps["plan-reviewer"].rules, vars=_BASE,
                              output={"blocking": False, "has_minor_findings": True})
    assert pr_minor.action.target == "plan-writer" and pr_minor.action.resume is True
    assert "plan_index" not in pr_minor.var_mutations
    tl = evaluate_rules(steps["task-lead"].rules, vars=_BASE,
                        output={"status": "Success", "done": False, "deviation": True,
                                "verify_tolerated": False, "summary": "x"})
    assert tl.action.target == "plan-writer" and tl.action.resume is True
    assert "plan_index" not in tl.var_mutations
