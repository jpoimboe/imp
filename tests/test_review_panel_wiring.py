"""Per-plan review wiring: autosquash + a single merged `review` step
(review-panel.md) run INSIDE the per-plan implement loop, followed by
deferred-sweep + sweep-verify + stamp-trailers; plan-cycle-check routes to the
whole-change review pass (review_scope=change) when multi_plan, then to
integrate-rebase. The integration-* steps and fast-path router gates are gone."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.prompts import build_context, render_prompt
from imp.assets import default_workflow_dir
from imp.engine.errors import LoadError
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.schema import validate_agent_output
from imp.engine.state import JsonlEventLog
from imp.engine.types import Continue, Goto

AGENTS = Path("src/imp/assets/workflows/default/agents")
_TIERS = frozenset({"low", "medium", "high"})

_BASE_VARS = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False, "panel_done": False,
    "multi_plan": False,
    "review_scope": "plan",
}


def _wf():
    return load_workflow(default_workflow_dir(), configured_tiers=_TIERS)


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_implement_phase_is_the_per_plan_loop() -> None:
    impl = next(p for p in _wf().phases if p.name == "implement")
    assert [s.name for s in impl.steps] == [
        "task-lead", "commit-check", "verify",
        "autosquash", "autosquash-resolve",
        "review", "deferred-sweep", "sweep-verify", "stamp-trailers",
        "plan-cycle-check",
    ]


def test_review_phase_starts_at_integrate_rebase() -> None:
    review = next(p for p in _wf().phases if p.name == "review")
    assert next(s.name for s in review.steps) == "integrate-rebase"
    names = {s.name for s in _wf().flat_steps()}
    for gone in ("integration-gate", "integration-pass", "integration-sweep",
                 "integration-sweep-verify", "stamp-sweep-trailers"):
        assert gone not in names, gone


def test_standalone_panel_gate_and_recheck_are_gone() -> None:
    names = {s.name for s in _wf().flat_steps()}
    assert "panel-gate" not in names and "recheck" not in names


def test_review_step_uses_review_panel_at_medium_tier() -> None:
    review = _steps(_wf())["review"]
    assert review.kind == "agent" and review.tier == "medium"
    assert review.file.name == "review-panel.md"
    assert review.differ_from is None


def test_review_step_routes_rework_to_plan_writer_and_sets_panel_done() -> None:
    rules = _steps(_wf())["review"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "verdict": "rework", "findings": "x"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.action.resume is False                      # cross-phase plain goto
    assert m.action.feedback                             # findings forwarded as a preamble
    assert m.var_mutations["panel_done"] is True
    m_ok = evaluate_rules(rules, vars=_BASE_VARS,
                          output={"status": "Success", "verdict": "approve", "findings": "x"})
    assert isinstance(m_ok.action, Continue)             # approve -> deferred-sweep
    assert m_ok.var_mutations["panel_done"] is True


def test_review_parse_contract_routes_declared_schema() -> None:
    wf = _wf()
    schema = wf.agents["review-panel"].output_schema
    rules = _steps(wf)["review"].rules
    for verdict in ("approve", "rework"):
        payload = {"status": "Success", "verdict": verdict, "findings": "x"}
        validate_agent_output(payload, schema)           # must not raise
        assert evaluate_rules(rules, vars=_BASE_VARS, output=payload).action is not None


def test_review_panel_verdict_enum_is_collapsed() -> None:
    schema = _wf().agents["review-panel"].output_schema
    verdict_field = next(f for f in schema.fields if f.name == "verdict")
    assert set(verdict_field.values) == {"approve", "rework"}
    for gap in ("task_gap", "plan_gap"):
        with pytest.raises(LoadError):
            validate_agent_output(
                {"status": "Success", "verdict": gap, "findings": "x"}, schema)


def test_no_gap_verdict_in_workflow_yaml() -> None:
    text = (AGENTS.parent / "workflow.yaml").read_text()
    assert "task_gap" not in text
    assert "plan_gap" not in text


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


def test_deferred_sweep_step_then_sweep_verify() -> None:
    steps = _steps(_wf())
    sweep = steps["deferred-sweep"]
    assert sweep.kind == "agent" and sweep.file.name == "deferred-sweep.md"
    assert any(r.mode == "allow" and "write" in r.operations for r in sweep.fs_permissions)
    st = steps["sweep-verify"]
    assert st.kind == "script" and st.command.endswith("run-verify.sh")
    assert isinstance(st.rules[-1].action, Goto)
    assert st.rules[-1].action.target == "deferred-sweep" and st.rules[-1].action.resume is True


def test_plan_cycle_check_routes_supercycle_wholechange_and_merge() -> None:
    rules = _steps(_wf())["plan-cycle-check"].rules
    # more_plans -> supercycle back to plan-writer
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "more_plans": True, "rationale": "x"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.var_mutations["panel_done"] is False and m.var_mutations["multi_plan"] is True
    # no more plans, multi-plan, per-plan scope just finished -> whole-change pass
    m2 = evaluate_rules(rules, vars={**_BASE_VARS, "multi_plan": True, "review_scope": "plan"},
                        output={"status": "Success", "more_plans": False, "rationale": "x"})
    assert isinstance(m2.action, Goto) and m2.action.target == "review"
    assert m2.var_mutations["review_scope"] == "change"
    # whole-change pass already ran (review_scope==change) -> integrate-rebase
    m3 = evaluate_rules(rules, vars={**_BASE_VARS, "multi_plan": True, "review_scope": "change"},
                        output={"status": "Success", "more_plans": False, "rationale": "x"})
    assert isinstance(m3.action, Goto) and m3.action.target == "integrate-rebase"
    # single-plan change -> straight to integrate-rebase
    m4 = evaluate_rules(rules, vars={**_BASE_VARS, "multi_plan": False},
                        output={"status": "Success", "more_plans": False, "rationale": "x"})
    assert isinstance(m4.action, Goto) and m4.action.target == "integrate-rebase"


def test_review_panel_reads_review_scope_not_integration_pass() -> None:
    text = (AGENTS / "review-panel.md").read_text()
    assert "integration_pass" not in text
    assert "review_scope" in text


def test_integrator_is_a_rebase_conflict_resolver() -> None:
    text = (AGENTS / "integrator.md").read_text()
    assert "MERGE_HEAD" not in text
    assert "--amend" not in text
    assert "git rebase --continue" in text
    assert "integration-notes.md" in text
    assert "integration: {type: enum" in text
    assert "wait_for_user" in text


def test_slices_resolve_from_asset_dir() -> None:
    from imp.evals.driver import baseline_agent_path
    for name in ("completeness-slice", "spec-compliance-slice", "boundaries-slice"):
        resolved = baseline_agent_path(name).resolve()
        assert resolved == (AGENTS / f"{name}.md").resolve()   # resolve both sides: cwd-independent
        assert resolved.exists()
    evals_agents = Path("src/imp/evals/data/agents")
    for name in ("completeness-slice", "spec-compliance-slice", "boundaries-slice"):
        assert not (evals_agents / f"{name}.md").exists()


def _render_panel(tmp_path, *, panel_done: bool, fast_path: bool,
                  review_scope: str = "plan") -> str:
    wf = _wf()
    body = wf.agents["review-panel"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(change_slug="t", change_title="t", event_log=log,
                        vars={"panel_done": panel_done, "fast_path": fast_path,
                              "review_scope": review_scope})
    return render_prompt(body, ctx)


def test_panel_done_reverify_dispatches_high_tier(tmp_path) -> None:
    out = _render_panel(tmp_path, panel_done=True, fast_path=False)
    assert 'task(subagent_name="code-reviewer", tier="high"' in out
    assert "spec-compliance-slice" not in out      # single reviewer, not the 4-member panel


def test_fast_path_reverify_stays_medium_tier(tmp_path) -> None:
    out = _render_panel(tmp_path, panel_done=False, fast_path=True)
    assert 'task(subagent_name="code-reviewer", tier="medium"' in out
    assert 'tier="high"' not in out


def test_first_pass_dispatches_full_four_member_panel_at_high(tmp_path) -> None:
    out = _render_panel(tmp_path, panel_done=False, fast_path=False)
    for member in ("code-reviewer", "completeness-slice",
                   "spec-compliance-slice", "boundaries-slice"):
        assert f'task(subagent_name="{member}", tier="high"' in out


def test_plan_writer_recognizes_rework_preambles() -> None:
    by = _steps(_wf())
    review_fb = by["review"].rules[0].action.feedback        # rework -> plan-writer
    finalize_fb = by["finalize"].rules[1].action.feedback    # decision: rework rule
    assert review_fb and finalize_fb
    text = (AGENTS / "plan-writer.md").read_text()
    assert review_fb in text
    assert finalize_fb in text


def test_finalize_rework_routes_to_plan_writer() -> None:
    rules = _steps(_wf())["finalize"].rules
    base = {"status": "Success", "built": "", "verify_status": "",
            "open_items": "", "next_steps": "", "comments": ""}
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={**base, "decision": "rework", "comments": "fix X"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.var_mutations["merge_approved"] is False
    m_merge = evaluate_rules(rules, vars=_BASE_VARS,
                             output={**base, "decision": "merge"})
    assert isinstance(m_merge.action, Goto) and m_merge.action.target == "merge"
    assert m_merge.var_mutations["merge_approved"] is True


def test_finalize_clarifies_before_rework_and_drops_implement_wording() -> None:
    text = (AGENTS / "finalize.md").read_text()
    assert "send the change back to implement" not in text
    assert "carried to the implementer" not in text
    assert "clarify the desired outcome and scope before returning a rework decision" in text


def test_task_lead_step0_drops_rerouted_feedback_sources() -> None:
    text = (AGENTS / "task-lead.md").read_text()
    # "review panel" alone also appears out-of-scope at line 82, so guard on the
    # Step-0-unique enumeration phrase that the edit removes:
    assert "review panel or the recheck reviewer" not in text   # review rework now -> plan-writer
    assert "human finalize decision" not in text                # human rework now -> plan-writer
    # the gate-failure resume source is still authoritative in Step 0
    assert 'gate failure (the `commit-check`' in text


def test_rework_path_loop_budgets_unchanged() -> None:
    steps = _steps(_wf())
    assert steps["review"].max_loops == 6
    assert steps["finalize"].max_loops == 20
