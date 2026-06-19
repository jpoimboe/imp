"""Per-batch review cadence: autosquash + review-base-init runs INSIDE the
per-plan implement loop; plan-cycle-check decides coverage AND cadence —
single `code-reviewer` per batch (review_base..HEAD), full 4-member opus
panel at change end (review_scope=change, merge-base..HEAD). deferred-sweep
+ sweep-verify + review-base-repin trail each review batch. stamp-trailers
is the first step of the review phase."""
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
from imp.engine.types import Continue, Goto

AGENTS = Path("src/imp/assets/workflows/imp/agents")
_TIERS = frozenset({"low", "medium", "high"})

_BASE_VARS = {
    "fast_path": False, "spec_minors_integrated": False,
    "plan_minors_integrated": False, "plan_panel_done": False,
    "change_panel_done": False,
    "review_scope": "plan",
    "review_base": "", "tasks_since_review": 0, "review_batch_size": 7,
}


def _wf():
    return load_workflow(bundled_workflow_dir("imp"), configured_tiers=_TIERS)


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_implement_phase_is_the_per_batch_loop() -> None:
    impl = next(p for p in _wf().phases if p.name == "implement")
    assert [s.name for s in impl.steps] == [
        "task-lead", "commit-check", "verify",
        "autosquash", "autosquash-resolve",
        "review-base-init", "plan-cycle-check",
        "review", "deferred-sweep", "sweep-verify", "review-base-repin",
    ]


def test_review_phase_is_stamp_trailers_then_finalize() -> None:
    # Merge-queue: the review phase only stamps trailers and takes the merge
    # approval; integration moves to the at-head merge phase, so integrate-rebase /
    # integrator / integrate-verify / repin-after-rebase no longer live here.
    review = next(p for p in _wf().phases if p.name == "review")
    assert [s.name for s in review.steps] == ["stamp-trailers", "finalize"]
    names = {s.name for s in _wf().flat_steps()}
    for gone in ("integration-gate", "integration-pass", "integration-sweep",
                 "integration-sweep-verify", "stamp-sweep-trailers",
                 "repin-after-rebase", "base-fresh-check"):
        assert gone not in names, gone


def test_standalone_panel_gate_and_recheck_are_gone() -> None:
    names = {s.name for s in _wf().flat_steps()}
    assert "panel-gate" not in names and "recheck" not in names


def test_review_step_uses_review_panel_at_medium_tier() -> None:
    review = _steps(_wf())["review"]
    assert review.kind == "agent" and review.tier == "medium"
    assert review.file.name == "review-panel.md"
    assert review.differ_from is None


def test_review_step_routes_two_way_no_plan_gap() -> None:
    rules = _steps(_wf())["review"].rules
    m_task = evaluate_rules(rules, vars=_BASE_VARS,
                            output={"status": "Success", "verdict": "task_gap", "findings": "x"})
    assert isinstance(m_task.action, Goto) and m_task.action.target == "task-lead"
    assert m_task.action.resume is False
    assert m_task.action.feedback
    assert "panel_done" not in m_task.var_mutations
    m_ok = evaluate_rules(rules, vars=_BASE_VARS,
                          output={"status": "Success", "verdict": "approve", "findings": "x"})
    assert isinstance(m_ok.action, Continue)
    assert m_ok.var_mutations == {}


def test_review_parse_contract_routes_declared_schema() -> None:
    wf = _wf()
    schema = wf.agents["review-panel"].output_schema
    rules = _steps(wf)["review"].rules
    for verdict in ("approve", "task_gap"):
        payload = {"status": "Success", "verdict": verdict, "findings": "x"}
        validate_agent_output(payload, schema)
        assert evaluate_rules(rules, vars=_BASE_VARS, output=payload).action is not None


def test_review_panel_verdict_enum_is_two_way() -> None:
    schema = _wf().agents["review-panel"].output_schema
    verdict_field = next(f for f in schema.fields if f.name == "verdict")
    assert set(verdict_field.values) == {"approve", "task_gap"}
    with pytest.raises(LoadError):
        validate_agent_output(
            {"status": "Success", "verdict": "plan_gap", "findings": "x"}, schema)


def test_review_panel_adjudicates_not_tallies() -> None:
    text = (AGENTS / "review-panel.md").read_text()
    assert "COMPUTE, do not judge" not in text
    assert "receiving-code-review" in text
    low = text.lower()
    # §2 guardrail
    assert "non-dismissable" in low
    assert "misclassification" in low
    assert "downward" in low
    # §3 routing taxonomy + tie-breaker
    assert "default to defer" in low
    assert "task_gap" in text          # plan_gap is removed: review no longer judges coverage
    assert "plan_gap" not in text


def test_gap_verdicts_route_in_workflow_yaml() -> None:
    text = (AGENTS.parent / "workflow.yaml").read_text()
    assert "task_gap" in text
    assert "plan_gap" not in text       # the plan_gap coverage route is gone
    assert "verdict: rework" not in text


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


def test_plan_cycle_check_routes_cadence_and_coverage() -> None:
    rules = _steps(_wf())["plan-cycle-check"].rules
    # batch full (more scope remains AND counter crossed N) -> per-batch review
    m_batch = evaluate_rules(rules, vars=_BASE_VARS,
                             output={"status": "Success", "more_plans": True,
                                     "running_tasks": 7, "rationale": "x"})
    assert isinstance(m_batch.action, Goto) and m_batch.action.target == "review"
    assert m_batch.var_mutations["tasks_since_review"] == 7
    assert m_batch.var_mutations["review_scope"] == "plan"  # forced: per-batch review
    # supercycle: more scope, batch NOT full -> plan-writer
    m_super = evaluate_rules(rules, vars=_BASE_VARS,
                             output={"status": "Success", "more_plans": True,
                                     "running_tasks": 4, "rationale": "x"})
    assert isinstance(m_super.action, Goto) and m_super.action.target == "plan-writer"
    assert m_super.action.feedback
    assert m_super.var_mutations["tasks_since_review"] == 4
    assert m_super.var_mutations["review_scope"] == "plan"  # all more-plans paths reset scope
    # trivial fast path: no panel -> straight to stamp-trailers (review phase head)
    m_fast = evaluate_rules(rules, vars={**_BASE_VARS, "fast_path": True},
                            output={"status": "Success", "more_plans": False,
                                    "running_tasks": 1, "rationale": "x"})
    assert isinstance(m_fast.action, Goto) and m_fast.action.target == "stamp-trailers"
    # coverage complete (else) -> end-of-change full panel
    m_cov = evaluate_rules(rules, vars=_BASE_VARS,
                           output={"status": "Success", "more_plans": False,
                                   "running_tasks": 3, "rationale": "x"})
    assert isinstance(m_cov.action, Goto) and m_cov.action.target == "review"
    assert m_cov.var_mutations["review_scope"] == "change"
    # more_plans:false wins over a full counter (a full final batch goes to the panel,
    # never to a redundant per-batch review then the panel)
    m_final = evaluate_rules(rules, vars=_BASE_VARS,
                             output={"status": "Success", "more_plans": False,
                                     "running_tasks": 99, "rationale": "x"})
    assert isinstance(m_final.action, Goto) and m_final.action.target == "review"
    assert m_final.var_mutations["review_scope"] == "change"


def test_review_panel_reads_review_scope_not_integration_pass() -> None:
    text = (AGENTS / "review-panel.md").read_text()
    assert "integration_pass" not in text
    assert "review_scope" in text
    assert "panel_done" not in text                 # the re-verify dial is gone
    assert "review_base" in text                     # per-batch diff base is pinned


def test_integrator_is_a_rebase_conflict_resolver() -> None:
    text = (AGENTS / "integrator.md").read_text()
    assert "MERGE_HEAD" not in text
    assert "--amend" not in text
    assert "git rebase --continue" in text
    assert "integration-notes.md" in text
    assert "integration: {type: enum" in text
    assert "wait_for_user" in text


def _render_panel(tmp_path, *, review_scope: str, review_base: str = "abc123") -> str:
    wf = _wf()
    body = wf.agents["review-panel"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(change_handle="t", change_title="t", event_log=log,
                        vars={"review_scope": review_scope, "review_base": review_base,
                              "fast_path": False})
    return render_prompt(body, ctx)


def test_end_of_change_dispatches_full_four_member_panel(tmp_path) -> None:
    out = _render_panel(tmp_path, review_scope="change")
    for member in ("code-reviewer", "completeness-slice",
                   "spec-compliance-slice", "boundaries-slice"):
        assert f'task(subagent_name="{member}", tier="high"' in out
    assert "git merge-base" in out                 # whole-change diff, never review_base
    assert "vars.review_base" not in out and "abc123" not in out


def test_per_batch_dispatches_single_code_reviewer_over_review_base(tmp_path) -> None:
    out = _render_panel(tmp_path, review_scope="plan")
    assert 'task(subagent_name="code-reviewer", tier="high"' in out
    assert "spec-compliance-slice" not in out      # single reviewer, not the panel
    assert "abc123..HEAD" in out                    # diff range pins review_base


def test_review_base_init_pins_once_then_idempotent() -> None:
    rules = _steps(_wf())["review-base-init"].rules
    m_first = evaluate_rules(rules, vars={**_BASE_VARS, "review_base": ""},
                             output={"exit": 0, "stdout": "fork123", "stderr": ""})
    assert isinstance(m_first.action, Continue)
    assert m_first.var_mutations["review_base"] == "fork123"
    m_later = evaluate_rules(rules, vars={**_BASE_VARS, "review_base": "head999"},
                             output={"exit": 0, "stdout": "fork123", "stderr": ""})
    assert isinstance(m_later.action, Continue)
    assert "review_base" not in m_later.var_mutations      # already pinned -> untouched


def test_review_base_repin_resets_counter_and_routes_by_scope() -> None:
    rules = _steps(_wf())["review-base-repin"].rules
    # end-of-change pass -> review phase head (stamp-trailers); counter reset; base advanced
    m_change = evaluate_rules(rules, vars={**_BASE_VARS, "review_scope": "change",
                                           "tasks_since_review": 9},
                              output={"exit": 0, "stdout": "deadbeef", "stderr": ""})
    assert isinstance(m_change.action, Goto) and m_change.action.target == "stamp-trailers"
    assert m_change.var_mutations["tasks_since_review"] == 0
    assert m_change.var_mutations["review_base"] == "deadbeef"
    # per-batch pass -> plan-writer for the next plan; counter reset; base advanced
    m_batch = evaluate_rules(rules, vars={**_BASE_VARS, "review_scope": "plan",
                                          "tasks_since_review": 9},
                             output={"exit": 0, "stdout": "cafef00d", "stderr": ""})
    assert isinstance(m_batch.action, Goto) and m_batch.action.target == "plan-writer"
    assert m_batch.var_mutations["tasks_since_review"] == 0
    assert m_batch.var_mutations["review_base"] == "cafef00d"


def test_plan_writer_recognizes_rework_preambles() -> None:
    by = _steps(_wf())
    finalize_fb = by["finalize"].rules[1].action.feedback    # decision: rework rule
    review_fb = by["review"].rules[0].action.feedback        # task_gap -> task-lead
    assert finalize_fb and review_fb
    text = (AGENTS / "plan-writer.md").read_text()
    assert finalize_fb in text


def test_finalize_rework_routes_to_plan_writer() -> None:
    rules = _steps(_wf())["finalize"].rules
    base = {"status": "Success", "built": "", "verify_status": "",
            "open_items": "", "next_steps": "", "comments": ""}
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={**base, "decision": "rework", "comments": "fix X"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    m_merge = evaluate_rules(rules, vars=_BASE_VARS,
                             output={**base, "decision": "merge"})
    assert isinstance(m_merge.action, Goto) and m_merge.action.target == "wait-for-head"


def test_finalize_clarifies_before_rework_and_drops_implement_wording() -> None:
    text = (AGENTS / "finalize.md").read_text()
    assert "send the change back to implement" not in text
    assert "carried to the implementer" not in text
    assert "clarify the desired outcome and scope before returning a rework decision" in text


def test_task_lead_step0_admits_panel_task_gap() -> None:
    text = (AGENTS / "task-lead.md").read_text()
    # the panel's task_gap rework is a recognized Step-0 feedback source again,
    # matching the exact preamble workflow.yaml emits for verdict: task_gap
    assert "The review panel found an implemented task needs rework." in text
    # the gate-failure resume source is still authoritative in Step 0
    assert 'gate failure (the `commit-check`' in text
    # human finalize rework still routes to plan-writer, not here
    assert "human finalize decision" not in text


def test_rework_path_loop_budgets_unchanged() -> None:
    steps = _steps(_wf())
    assert steps["review"].max_loops == 6
    assert steps["finalize"].max_loops == 20


PANEL_MEMBERS = ("code-reviewer", "completeness-slice",
                 "spec-compliance-slice", "boundaries-slice")


@pytest.mark.parametrize("name", PANEL_MEMBERS)
def test_panel_members_are_pure_advisors(name: str) -> None:
    schema = _wf().agents[name].output_schema
    fields = {f.name for f in schema.fields}
    assert "blocking" in fields and "verdict" not in fields
    assert "findings" in fields
    text = (AGENTS / f"{name}.md").read_text()
    assert "verdict" not in text
    assert "task_gap" not in text
    assert "plan_gap" not in text
    assert "approve" not in text
