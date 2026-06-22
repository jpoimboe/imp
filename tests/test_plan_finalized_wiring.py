from __future__ import annotations

from pathlib import Path

from imp.assets import bundled_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto

AGENTS = Path("src/imp/assets/workflows/imp/agents")

_BASE = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False, "review_scope": "plan",
    "review_base": "",
    "change_panel_done": False, "plan_index": 1, "plan_total": 0, "task_total": 0,
}


def _steps():
    wf = load_workflow(bundled_workflow_dir("imp"))
    return {s.name: s for s in wf.flat_steps()}


def test_plan_finalized_step_is_a_medium_agent() -> None:
    step = _steps()["plan-finalized"]
    assert step.kind == "agent" and step.tier == "medium"
    assert step.file.name == "plan-finalized.md"


def test_plan_finalized_sets_task_total_and_enters_implement() -> None:
    rules = _steps()["plan-finalized"].rules
    m = evaluate_rules(rules, vars=_BASE, output={"status": "Success", "task_total": 6})
    assert isinstance(m.action, Goto) and m.action.target == "task-lead"
    assert m.var_mutations["task_total"] == 6


def test_fresh_plan_arrows_route_through_plan_finalized() -> None:
    steps = _steps()
    fast = evaluate_rules(steps["plan-verify-commit"].rules, vars={**_BASE, "fast_path": True},
                          output={"exit": 0, "stdout": "", "stderr": ""})
    assert fast.action.target == "plan-finalized"
    minors = evaluate_rules(steps["plan-reviewer"].rules,
                            vars={**_BASE, "plan_minors_integrated": True},
                            output={"blocking": False, "has_minor_findings": True})
    assert minors.action.target == "plan-finalized"
    clean = evaluate_rules(steps["plan-reviewer"].rules, vars=_BASE,
                           output={"blocking": False, "has_minor_findings": False})
    assert clean.action.target == "plan-finalized"


def test_task_loop_and_rework_bypass_plan_finalized() -> None:
    steps = _steps()
    # task-lead's normal completion continues to verify, never back through plan-finalized.
    loop = evaluate_rules(steps["task-lead"].rules, vars=_BASE,
                          output={"status": "Success", "done": True, "deviation": False,
                                  "verify_tolerated": False, "summary": "x"})
    assert isinstance(loop.action, Continue)
    # code-reviewer's task_gap rework re-enters task-lead directly, not plan-finalized.
    gap = evaluate_rules(steps["code-reviewer"].rules, vars=_BASE,
                         output={"status": "Success", "verdict": "task_gap", "findings": "x"})
    assert gap.action.target == "task-lead"


def test_plan_finalized_agent_counts_and_announces() -> None:
    text = (AGENTS / "plan-finalized.md").read_text()
    assert "task_total: {type: int}" in text
    assert "## Task" in text          # counts task headings
    assert "emit_notice" in text      # announces the list once
