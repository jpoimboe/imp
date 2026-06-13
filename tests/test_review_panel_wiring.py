"""Per-plan review wiring: autosquash/stamp-trailers + a single merged `review`
step (review-panel.md) run INSIDE the per-plan implement loop, followed by
deferred-sweep + a post-sweep run-tests; the review phase begins with a trimmed
end-of-change integration pass gated on multi_plan. The standalone panel-gate/
recheck steps are gone; the review step tiers internally on workflow vars
(panel_done/fast_path/integration_pass), which is prose and dogfood-verified."""
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
    "panel_done": False, "integration_needs_review": False,
    "spec_confirm": True, "spec_unseen_changes": False,
    "spec_gate_pending": False, "spec_gate_passed": False,
    "multi_plan": False, "integration_pass": False,
}


def _wf():
    return load_workflow(default_workflow_dir(), configured_tiers=_TIERS)


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_implement_phase_is_the_per_plan_loop() -> None:
    impl = next(p for p in _wf().phases if p.name == "implement")
    assert [s.name for s in impl.steps] == [
        "task-lead", "commit-check", "run-tests",
        "autosquash", "autosquash-resolve", "autosquash-verify", "stamp-trailers",
        "review", "deferred-sweep", "sweep-tests",
        "fast-path-cycle-gate", "plan-cycle-check",
    ]


def test_review_phase_runs_integration_pass_then_merge() -> None:
    review = next(p for p in _wf().phases if p.name == "review")
    assert [s.name for s in review.steps][:6] == [
        "integration-gate", "integration-pass", "integration-sweep",
        "integration-sweep-tests", "stamp-sweep-trailers", "integrate-merge",
    ]


def test_standalone_panel_gate_and_recheck_are_gone() -> None:
    names = {s.name for s in _wf().flat_steps()}
    assert "panel-gate" not in names and "recheck" not in names


def test_review_step_uses_review_panel_at_medium_tier() -> None:
    review = _steps(_wf())["review"]
    assert review.kind == "agent" and review.tier == "medium"
    assert review.file.name == "review-panel.md"
    assert review.differ_from is None


def test_review_step_routes_and_sets_panel_done() -> None:
    rules = _steps(_wf())["review"].rules
    for verdict, target in [("task_gap", "task-lead"), ("plan_gap", "plan-writer")]:
        m = evaluate_rules(rules, vars=_BASE_VARS,
                           output={"status": "Success", "verdict": verdict, "findings": "x"})
        assert isinstance(m.action, Goto) and m.action.target == target, verdict
        assert m.var_mutations["panel_done"] is True, verdict
    m_ok = evaluate_rules(rules, vars=_BASE_VARS,
                          output={"status": "Success", "verdict": "approve", "findings": "x"})
    assert isinstance(m_ok.action, Continue)            # approve -> deferred-sweep
    assert m_ok.var_mutations["panel_done"] is True


def test_review_parse_contract_routes_declared_schema() -> None:
    wf = _wf()
    schema = wf.agents["review-panel"].output_schema
    rules = _steps(wf)["review"].rules
    for verdict in ("task_gap", "plan_gap", "approve"):
        payload = {"status": "Success", "verdict": verdict, "findings": "x"}
        validate_agent_output(payload, schema)          # must not raise
        assert evaluate_rules(rules, vars=_BASE_VARS, output=payload).action is not None


def test_review_panel_declares_four_members() -> None:
    assert set(_wf().agents["review-panel"].subagents) == {
        "code-reviewer", "completeness-slice", "spec-compliance-slice", "boundaries-slice"}


def test_review_step_can_write_progress_md() -> None:
    wf = _wf()
    assert "write_file" in wf.agents["review-panel"].tools
    review = _steps(wf)["review"]
    assert any(r.mode == "allow" and "write" in r.operations
               for r in review.fs_permissions)


def test_review_panel_pins_deferred_observation_format() -> None:
    # AC 8: review-panel is a producer of the Deferred Followups list and MUST
    # append in the exact pinned format the sweep parses. Guard the prose the same
    # way task-lead's pinned format is guarded.
    text = (AGENTS / "review-panel.md").read_text()
    assert "- [ ] DEFERRED review-panel:" in text
    assert "[scope:" in text
    assert "deferred-sweep" in text.lower()   # names the consumer step


def test_deferred_sweep_step_then_sweep_tests() -> None:
    steps = _steps(_wf())
    sweep = steps["deferred-sweep"]
    assert sweep.kind == "agent" and sweep.file.name == "deferred-sweep.md"
    assert any(r.mode == "allow" and "write" in r.operations for r in sweep.fs_permissions)
    st = steps["sweep-tests"]
    assert st.kind == "script" and st.command.endswith("run-tests.sh")
    assert isinstance(st.rules[-1].action, Goto)
    assert st.rules[-1].action.target == "deferred-sweep" and st.rules[-1].action.resume is True


def test_plan_cycle_check_resets_panel_done_and_sets_multi_plan() -> None:
    rules = _steps(_wf())["plan-cycle-check"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "more_plans": True, "rationale": "x"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.var_mutations["panel_done"] is False and m.var_mutations["multi_plan"] is True


def test_integration_gate_gates_on_multi_plan() -> None:
    rules = _steps(_wf())["integration-gate"].rules
    m_single = evaluate_rules(rules, vars={**_BASE_VARS, "multi_plan": False}, output=None)
    assert isinstance(m_single.action, Goto) and m_single.action.target == "stamp-sweep-trailers"
    m_multi = evaluate_rules(rules, vars={**_BASE_VARS, "multi_plan": True}, output=None)
    assert isinstance(m_multi.action, Continue)
    assert m_multi.var_mutations["integration_pass"] is True


def test_integration_pass_uses_review_panel_and_routes_task_gap() -> None:
    ip = _steps(_wf())["integration-pass"]
    assert ip.kind == "agent" and ip.file.name == "review-panel.md"
    m_gap = evaluate_rules(ip.rules, vars={**_BASE_VARS, "integration_pass": True},
                           output={"status": "Success", "verdict": "task_gap", "findings": "x"})
    assert isinstance(m_gap.action, Goto) and m_gap.action.target == "task-lead"
    assert m_gap.var_mutations["integration_pass"] is False
    m_ok = evaluate_rules(ip.rules, vars={**_BASE_VARS, "integration_pass": True},
                          output={"status": "Success", "verdict": "approve", "findings": "x"})
    assert isinstance(m_ok.action, Continue)
    assert m_ok.var_mutations["integration_pass"] is False


def test_integration_sweep_reuses_deferred_sweep_and_stamps() -> None:
    steps = _steps(_wf())
    assert steps["integration-sweep"].file.name == "deferred-sweep.md"
    st = steps["integration-sweep-tests"]
    assert st.command.endswith("run-tests.sh")
    assert st.rules[-1].action.target == "integration-sweep"
    stamp = steps["stamp-sweep-trailers"]
    assert stamp.kind == "script" and stamp.command.endswith("stamp-trailers.sh")


def test_slices_resolve_from_asset_dir() -> None:
    from imp.evals.driver import baseline_agent_path
    for name in ("completeness-slice", "spec-compliance-slice", "boundaries-slice"):
        resolved = baseline_agent_path(name).resolve()
        assert resolved == (AGENTS / f"{name}.md").resolve()   # resolve both sides: cwd-independent
        assert resolved.exists()
    evals_agents = Path("src/imp/evals/data/agents")
    for name in ("completeness-slice", "spec-compliance-slice", "boundaries-slice"):
        assert not (evals_agents / f"{name}.md").exists()
