"""The per-plan implement loop carries no code review or sweep; the single
change-wide code review (`change-review`, reusing plan-code-reviewer.md) runs once
in the converge phase after gap-check latches coverage, paired with the
once-per-change deferred sweep."""
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.errors import LoadError
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.schema import validate_agent_output
from imp.engine.state import JsonlEventLog
from imp.engine.types import Continue, Goto, Pause

AGENTS = Path("src/imp/assets/workflows/imp/agents")
_TIERS = frozenset({"low", "medium", "high"})

_BASE_VARS = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False,
    "coverage_settled": False,
    "plan_index": 1, "plan_total": 0, "task_total": 0,
}


def _wf():
    return load_workflow(bundled_workflow_dir("imp"), configured_tiers=_TIERS)


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_implement_phase_is_the_per_batch_loop() -> None:
    wf = _wf()
    impl = next(p for p in wf.phases if p.name == "implement")
    assert [s.name for s in impl.steps] == [
        "task-lead", "verify", "cleanup-implement",
        "autosquash-resolve-implement", "check-next-plan",
    ]


def test_review_sweep_and_finalize_left_the_implement_loop() -> None:
    wf = _wf()
    impl_names = [s.name for s in next(p for p in wf.phases if p.name == "implement").steps]
    assert impl_names[-1] == "check-next-plan"
    for gone in ("change-review", "plan-code-reviewer", "gap-check",
                 "deferred-sweep", "sweep-verify", "summarizer", "finalize"):
        assert gone not in impl_names, gone
    conv_names = [s.name for s in next(p for p in wf.phases if p.name == "converge").steps]
    assert conv_names == [
        "gap-check", "change-review", "deferred-sweep", "sweep-verify",
        "cleanup-sweep", "autosquash-resolve-sweep", "converge-verify",
    ]


def test_standalone_panel_gate_and_recheck_are_gone() -> None:
    names = {s.name for s in _wf().flat_steps()}
    assert "panel-gate" not in names and "recheck" not in names


def test_review_step_uses_code_reviewer_at_high_tier() -> None:
    wf = _wf()
    step = _steps(wf)["change-review"]
    assert step.tier == "high"
    assert step.file is not None and step.file.name == "plan-code-reviewer.md"


def test_review_step_routes_two_way_no_plan_gap() -> None:
    wf = _wf()
    rules = _steps(wf)["change-review"].rules
    gap = evaluate_rules(rules, vars=_BASE_VARS,
                         output={"status": "Success", "verdict": "task_gap"})
    assert isinstance(gap.action, Goto) and gap.action.target == "task-lead"
    ok = evaluate_rules(rules, vars=_BASE_VARS,
                        output={"status": "Success", "verdict": "approve"})
    assert isinstance(ok.action, Goto) and ok.action.target == "deferred-sweep"


def test_review_parse_contract_routes_declared_schema() -> None:
    wf = _wf()
    schema = wf.agents["plan-code-reviewer"].output_schema
    rules = _steps(wf)["change-review"].rules
    for verdict in ("approve", "task_gap"):
        payload = {"status": "Success", "verdict": verdict, "findings": "x"}
        validate_agent_output(payload, schema)
        assert evaluate_rules(rules, vars=_BASE_VARS, output=payload).action is not None


def test_plan_code_reviewer_verdict_enum_is_two_way() -> None:
    schema = _wf().agents["plan-code-reviewer"].output_schema
    verdict_field = next(f for f in schema.fields if f.name == "verdict")
    assert set(verdict_field.values) == {"approve", "task_gap"}
    assert all(f.name != "blocking" for f in schema.fields)  # blocking replaced by verdict
    with pytest.raises(LoadError):
        validate_agent_output(
            {"status": "Success", "verdict": "plan_gap", "findings": "x"}, schema)


def test_plan_code_reviewer_adjudicates_and_routes() -> None:
    text = (AGENTS / "plan-code-reviewer.md").read_text()
    low = text.lower()
    assert "receiving-code-review" in text
    # guardrail
    assert "non-dismissable" in low
    assert "misclassification" in low
    assert "downward" in low
    # routing taxonomy + tie-breaker
    assert "default to defer" in low
    assert "task_gap" in text          # review no longer judges coverage
    assert "plan_gap" not in text


def test_gap_verdicts_route_in_workflow_yaml() -> None:
    text = (AGENTS.parent / "workflow.yaml").read_text()
    assert "task_gap" in text
    assert "plan_gap" not in text
    assert "verdict: rework" not in text


def test_workflow_comment_describes_single_reviewer() -> None:
    text = (AGENTS.parent / "workflow.yaml").read_text()
    assert "agents/plan-code-reviewer.md" in text
    assert "agents/review-panel.md" not in text
    assert "opus" not in text          # the opus-panel framing is gone
    assert "4-member" not in text


def test_plan_code_reviewer_pins_deferred_observation_format() -> None:
    text = (AGENTS / "plan-code-reviewer.md").read_text()
    assert "- [ ] DEFERRED plan-code-reviewer:" in text
    assert "[scope:" in text
    assert "deferred-sweep" in text.lower()   # names the consumer step


def test_deferred_sweep_step_then_sweep_verify() -> None:
    steps = _steps(_wf())
    sweep = steps["deferred-sweep"]
    assert sweep.kind == "agent" and sweep.file.name == "deferred-sweep.md"
    st = steps["sweep-verify"]
    assert st.kind == "script" and st.command.endswith("run-verify.sh fast")
    assert isinstance(st.rules[-1].action, Goto)
    assert st.rules[-1].action.target == "deferred-sweep" and st.rules[-1].action.resume is True


def test_gap_check_routes_coverage_at_change_end() -> None:
    rules = _steps(_wf())["gap-check"].rules
    # More scope remains -> author a corrective via plan-writer; review_scope forced 'plan'.
    m_more = evaluate_rules(rules, vars={**_BASE_VARS, "plan_index": 2, "plan_total": 2},
                            output={"status": "Success", "more_plans": True,
                                    "rationale": "x", "plan_total": 3})
    assert isinstance(m_more.action, Goto) and m_more.action.target == "plan-writer"
    assert m_more.var_mutations["plan_total"] == 3
    assert m_more.var_mutations["plan_index"] == 3
    # Coverage complete + multi-plan -> change-wide review, latched.
    m_multi = evaluate_rules(rules, vars={**_BASE_VARS, "plan_total": 2},
                             output={"status": "Success", "more_plans": False,
                                     "rationale": "x", "plan_total": 2})
    assert isinstance(m_multi.action, Goto) and m_multi.action.target == "change-review"
    assert "review_scope" not in m_multi.var_mutations
    assert m_multi.var_mutations["coverage_settled"] is True
    # Coverage complete + single-plan -> change-wide review fires too (plan_total gate dropped).
    m_one = evaluate_rules(rules, vars={**_BASE_VARS, "plan_total": 1},
                           output={"status": "Success", "more_plans": False,
                                   "rationale": "x", "plan_total": 1})
    assert isinstance(m_one.action, Goto) and m_one.action.target == "change-review"
    assert "review_scope" not in m_one.var_mutations
    assert m_one.var_mutations["coverage_settled"] is True


def test_check_next_plan_routes_settled_batch_to_change_review() -> None:
    wf = _wf()
    ci = evaluate_rules(_steps(wf)["cleanup-implement"].rules, vars={}, output={"exit": 0})
    assert isinstance(ci.action, Goto) and ci.action.target == "check-next-plan"
    cnp = _steps(wf)["check-next-plan"].rules
    corrective = evaluate_rules(cnp, vars={**_BASE_VARS, "coverage_settled": True,
                                           "plan_index": 1, "plan_total": 1}, output={})
    assert isinstance(corrective.action, Goto) and corrective.action.target == "change-review"
    fresh = evaluate_rules(cnp, vars={**_BASE_VARS, "coverage_settled": False,
                                      "plan_index": 1, "plan_total": 1}, output={})
    assert isinstance(fresh.action, Goto) and fresh.action.target == "gap-check"


def test_plan_code_reviewer_reads_review_scope_not_integration_pass() -> None:
    text = (AGENTS / "plan-code-reviewer.md").read_text()
    assert "integration_pass" not in text
    assert "review_scope" in text
    assert "panel_done" not in text                 # the re-verify dial is gone
    assert "review_base" in text                     # per-batch diff base is pinned


def test_reconciler_is_a_rebase_conflict_resolver() -> None:
    text = (AGENTS / "reconciler.md").read_text()
    assert "MERGE_HEAD" not in text
    assert "--amend" not in text
    assert "git rebase --continue" in text
    assert "integration-notes.md" not in text
    assert "stuck: {type: bool}" in text
    assert "wait_for_user" not in text
    # AC9: still instructs the cur_base_head := B replant on the conflicted-rebase path
    assert "cur_base_head" in text


def _render_reviewer(tmp_path, *, review_scope: str, review_base: str = "abc123",
                     cur_base_head: str = "forkSHA") -> str:
    wf = _wf()
    body = wf.agents["plan-code-reviewer"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(change_title="t", event_log=log, cur_base_head=cur_base_head,
                        change_id="chg",
                        vars={"review_scope": review_scope, "review_base": review_base,
                              "fast_path": False})
    return render_prompt(body, ctx, workflow_dir=bundled_workflow_dir("imp"))


def test_end_of_change_reviews_whole_change_diff(tmp_path) -> None:
    out = _render_reviewer(tmp_path, review_scope="change")
    assert "forkSHA..imp/chg" in out                 # whole-change diff from recorded fork
    assert "git merge-base" not in out
    assert "vars.review_base" not in out and "abc123" not in out
    assert "task(subagent_name=" not in out         # reviews directly, dispatches nothing


def test_per_batch_reviews_review_base_range(tmp_path) -> None:
    out = _render_reviewer(tmp_path, review_scope="plan")
    assert "abc123..imp/chg" in out                 # diff range pins review_base
    assert "spec-compliance-slice" not in out       # single reviewer, no panel
    assert "task(subagent_name=" not in out


def test_per_batch_empty_review_base_falls_back_to_fork(tmp_path) -> None:
    # review-base-init is gone: the first per-batch review runs with review_base == "",
    # which must resolve to the recorded fork point, not a broken empty-LHS range.
    out = _render_reviewer(tmp_path, review_scope="plan", review_base="",
                           cur_base_head="forkSHA")
    assert "forkSHA..imp/chg" in out


def test_plan_writer_recognizes_rework_preambles() -> None:
    by = _steps(_wf())
    assert by["change-review"].rules[0].action.feedback
    assert by["finalize"].rules[1].action.feedback
    finalize_fb = by["finalize"].rules[1].action.feedback    # decision: rework rule
    text = (AGENTS / "plan-writer.md").read_text()
    assert finalize_fb in text


def test_finalize_rework_routes_to_plan_writer() -> None:
    rules = _steps(_wf())["finalize"].rules
    rework = evaluate_rules(rules, vars={"plan_index": 1, "plan_total": 1},
                            output={"decision": "rework"})
    assert isinstance(rework.action, Goto) and rework.action.target == "plan-writer"
    assert rework.action.release == "base"
    assert "review_scope" not in rework.var_mutations
    merge = evaluate_rules(rules, vars={"plan_index": 1, "plan_total": 1},
                           output={"decision": "merge"})
    assert isinstance(merge.action, Goto) and merge.action.target == "cleanup-merge"


def test_finalize_clarifies_before_rework_and_drops_implement_wording() -> None:
    text = (AGENTS / "finalize.md").read_text()
    assert "send the change back to implement" not in text
    assert "carried to the coder" not in text
    assert "clarify the desired outcome and scope before returning a rework decision" in text


def test_task_lead_step0_admits_panel_task_gap() -> None:
    text = (AGENTS / "task-lead.md").read_text()
    # the task_gap rework is a recognized Step-0 feedback source, matching the exact
    # preamble workflow.yaml emits for verdict: task_gap (kept verbatim — task-lead.md
    # is out of scope for this change).
    assert "The plan-code-reviewer found an implemented task needs rework." in text
    assert 'gate failure (the `verify`' in text
    assert "human finalize decision" not in text


def test_rework_path_loop_budgets_unchanged() -> None:
    steps = _steps(_wf())
    assert steps["change-review"].max_loops == 6
    assert steps["finalize"].max_loops == 20


def test_panel_and_slice_files_are_deleted() -> None:
    for gone in ("review-panel.md", "completeness-slice.md",
                 "spec-compliance-slice.md", "boundaries-slice.md"):
        assert not (AGENTS / gone).exists(), gone


def test_plan_code_reviewer_is_not_a_pure_advisor() -> None:
    text = (AGENTS / "plan-code-reviewer.md").read_text()
    low = text.lower()
    assert "pure advisor" not in low          # it routes now, not merely advises
    assert "verdict" in low                    # owns the keep/dismiss/defer call
    assert "approve" in text and "task_gap" in text


def test_cleanup_sweep_routes_by_exit_code() -> None:
    wf = _wf()
    step = _steps(wf)["cleanup-sweep"]
    assert step.kind == "script"
    assert step.command == "./scripts/cleanup.sh"
    m0 = evaluate_rules(step.rules, vars={}, output={"exit": 0})
    assert isinstance(m0.action, Goto) and m0.action.target == "converge-verify"
    m2 = evaluate_rules(step.rules, vars={}, output={"exit": 2})
    assert isinstance(m2.action, Continue)
    m3 = evaluate_rules(step.rules, vars={}, output={"exit": 3})
    assert isinstance(m3.action, Goto) and m3.action.target == "deferred-sweep"
    assert m3.action.resume is True
    m1 = evaluate_rules(step.rules, vars={}, output={"exit": 1})
    assert isinstance(m1.action, Pause)


def test_plan_code_reviewer_renders_canonical_severity_rubric(tmp_path) -> None:
    out = _render_reviewer(tmp_path, review_scope="plan")
    assert "behavioral impact, not file type" in out
    for term in ("**Critical**", "**Major**", "**Minor**", "**Nit**"):
        assert term in out
