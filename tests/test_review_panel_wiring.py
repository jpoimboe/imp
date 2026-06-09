"""Layer 2 (review-architecture spec): the review phase is panel-gate ->
review-panel (sonnet coordinator, opus members, runs ONCE) -> recheck (sonnet
loop). Deterministic wiring + parse-contract; the actual collapse quality is
dogfood-only (live model)."""
from __future__ import annotations

from pathlib import Path

from imp.assets import default_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.schema import validate_agent_output
from imp.engine.types import Continue, Goto

AGENTS = Path("src/imp/assets/workflows/default/agents")
_TIERS = frozenset({"low", "medium", "high"})

_BASE_VARS = {
    "fast_path": False, "ready_to_merge": False, "final_review": True,
    "spec_minors_integrated": False, "plan_minors_integrated": False,
    "panel_done": False,
}


def _wf():
    return load_workflow(default_workflow_dir(), configured_tiers=_TIERS)


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_review_phase_step_order() -> None:
    wf = _wf()
    review = next(p for p in wf.phases if p.name == "review")
    names = [s.name for s in review.steps]
    assert names == [
        "autosquash", "stamp-trailers", "panel-gate",
        "review-panel", "recheck",
        "integrate-merge", "integrator", "integrate-tests", "integration-review",
        "summarizer", "final-review-gate", "final-review",
    ], names


def test_panel_done_var_declared_default_false() -> None:
    assert _wf().vars.defaults()["panel_done"] is False


def test_panel_gate_is_branch_with_no_differ() -> None:
    gate = _steps(_wf())["panel-gate"]
    assert gate.kind == "branch"
    assert gate.differ_from is None


def test_review_panel_step_tiers_and_no_differ() -> None:
    steps = _steps(_wf())
    rp = steps["review-panel"]
    assert rp.kind == "agent"
    assert rp.tier == "medium"               # sonnet coordinator
    assert rp.file.name == "review-panel.md"
    assert rp.differ_from is None            # single provider: differ_from removed
    rc = steps["recheck"]
    assert rc.tier == "medium"               # sonnet verification
    assert rc.file.name == "code-reviewer.md"  # reuses the generalist prompt
    assert rc.max_loops == 5


def test_one_shot_gating() -> None:
    # panel_done=false -> fall through to review-panel; true -> skip to recheck.
    rules = _steps(_wf())["panel-gate"].rules
    m_first = evaluate_rules(rules, vars={**_BASE_VARS, "panel_done": False}, output=None)
    assert isinstance(m_first.action, Continue)
    m_again = evaluate_rules(rules, vars={**_BASE_VARS, "panel_done": True}, output=None)
    assert isinstance(m_again.action, Goto) and m_again.action.target == "recheck"


def test_fast_path_skips_opus_panel_to_recheck() -> None:
    # A trivial (fast_path) change skips the expensive opus panel entirely and
    # gets only the cheap sonnet recheck whole-diff review. panel_done stays false
    # (the panel never ran), so re-entry keeps routing fast-path -> recheck.
    rules = _steps(_wf())["panel-gate"].rules
    fp_vars = {**_BASE_VARS, "fast_path": True, "panel_done": False}
    m = evaluate_rules(rules, vars=fp_vars, output=None)
    assert isinstance(m.action, Goto) and m.action.target == "recheck"


def test_review_panel_routes_and_sets_panel_done() -> None:
    rules = _steps(_wf())["review-panel"].rules
    for verdict, target in [("task_gap", "implement"),
                            ("plan_gap", "plan-writer"),
                            ("approve", "integrate-merge")]:
        m = evaluate_rules(rules, vars=_BASE_VARS,
                           output={"status": "Success", "verdict": verdict, "findings": "x"})
        assert isinstance(m.action, Goto) and m.action.target == target, verdict
        assert m.var_mutations["panel_done"] is True, verdict


def test_recheck_reuses_code_reviewer_routing() -> None:
    rules = _steps(_wf())["recheck"].rules
    m_task = evaluate_rules(rules, vars=_BASE_VARS,
                            output={"status": "Success", "verdict": "task_gap", "findings": "x"})
    assert isinstance(m_task.action, Goto) and m_task.action.target == "implement"
    m_plan = evaluate_rules(rules, vars=_BASE_VARS,
                            output={"status": "Success", "verdict": "plan_gap", "findings": "x"})
    assert isinstance(m_plan.action, Goto) and m_plan.action.target == "plan-writer"
    m_ok = evaluate_rules(rules, vars=_BASE_VARS,
                          output={"status": "Success", "verdict": "approve", "findings": "x"})
    assert isinstance(m_ok.action, Continue)


def test_parse_contract_declared_schema_routes_correctly() -> None:
    # Guards the silent-approve risk (spec concern #5): review-panel.md's DECLARED
    # output schema must accept the verdict enum and route it through the reused
    # rules. A wrong field name or `required: false` would let rules fall through
    # to else-approve. This validates the declared schema, not runtime survival.
    wf = _wf()
    schema = wf.agents["review-panel"].output_schema
    rules = _steps(wf)["review-panel"].rules
    for verdict, target in [("task_gap", "implement"),
                            ("plan_gap", "plan-writer"),
                            ("approve", "integrate-merge")]:
        payload = {"status": "Success", "verdict": verdict, "findings": "x"}
        validate_agent_output(payload, schema)   # must not raise
        m = evaluate_rules(rules, vars=_BASE_VARS, output=payload)
        assert isinstance(m.action, Goto) and m.action.target == target, verdict


def test_review_panel_declares_four_members() -> None:
    subs = set(_wf().agents["review-panel"].subagents)
    assert subs == {"code-reviewer", "completeness-slice",
                    "spec-compliance-slice", "boundaries-slice"}, subs


def test_slices_resolve_from_asset_dir() -> None:
    from imp.evals.driver import baseline_agent_path
    for name in ("completeness-slice", "spec-compliance-slice", "boundaries-slice"):
        resolved = baseline_agent_path(name)
        assert resolved == (AGENTS / f"{name}.md").resolve() or resolved == AGENTS / f"{name}.md", (
            f"{name} did not resolve to the asset dir: {resolved}")
        assert resolved.exists()
    # The shadowed eval copies are deleted.
    evals_agents = Path("src/imp/evals/data/agents")
    for name in ("completeness-slice", "spec-compliance-slice", "boundaries-slice"):
        assert not (evals_agents / f"{name}.md").exists()
