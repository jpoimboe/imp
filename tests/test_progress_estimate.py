from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Goto

_BASE = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False, "review_scope": "plan",
    "review_base": "",
    "change_panel_done": False, "plan_index": 2, "plan_total": 4, "task_total": 0,
}


def _rules():
    wf = load_workflow(bundled_workflow_dir("imp"))
    return {s.name: s for s in wf.flat_steps()}["plan-cycle-check"].rules


def test_more_plans_carries_total_without_advancing_index() -> None:
    # In the per-plan model plan-cycle-check reviews THIS plan first (code-reviewer);
    # plan_index advances later at review-base-repin, not here.
    m = evaluate_rules(_rules(), vars=_BASE,
                       output={"status": "Success", "more_plans": True,
                               "rationale": "x", "plan_total": 5})
    assert isinstance(m.action, Goto) and m.action.target == "code-reviewer"
    assert m.var_mutations["plan_total"] == 5
    assert "plan_index" not in m.var_mutations


def test_coverage_complete_resolves_total_to_index() -> None:
    m = evaluate_rules(_rules(), vars={**_BASE, "plan_index": 3, "plan_total": 3},
                       output={"status": "Success", "more_plans": False,
                               "rationale": "x", "plan_total": 3})
    assert isinstance(m.action, Goto) and m.action.target == "code-reviewer"
    assert m.var_mutations["plan_total"] == 3
    assert "plan_index" not in m.var_mutations  # coverage-complete does not advance the plan
