from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Goto

_BASE = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False, "review_scope": "plan",
    "review_base": "",
    "coverage_settled": False, "plan_index": 2, "plan_total": 4, "task_total": 0,
}


def _rules():
    wf = load_workflow(bundled_workflow_dir("imp"))
    return {s.name: s for s in wf.flat_steps()}["gap-check"].rules


def test_more_plans_advances_index_and_carries_total() -> None:
    # gap-check authoring a corrective bumps plan_total and advances plan_index itself.
    m = evaluate_rules(_rules(), vars=_BASE,
                       output={"status": "Success", "more_plans": True,
                               "rationale": "x", "plan_total": 5})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.var_mutations["plan_total"] == 5
    assert m.var_mutations["plan_index"] == 3


def test_coverage_complete_latches_and_does_not_advance_index() -> None:
    m = evaluate_rules(_rules(), vars={**_BASE, "plan_index": 3, "plan_total": 3},
                       output={"status": "Success", "more_plans": False,
                               "rationale": "x", "plan_total": 3})
    assert isinstance(m.action, Goto) and m.action.target == "plan-code-reviewer"
    assert m.var_mutations["coverage_settled"] is True
    assert "plan_index" not in m.var_mutations
