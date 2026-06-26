from __future__ import annotations

from pathlib import Path

from imp.assets import bundled_workflow_dir, bundled_workflow_names
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

FLAT = Path("src/imp/assets/workflows/imp-flat")
AGENTS = FLAT / "agents"


def _flat():
    return load_workflow(bundled_workflow_dir("imp-flat"))


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


# All declared imp-flat vars at their defaults — a realistic evaluate_rules fixture.
_FLAT_VARS = {
    "fast_path": False,
    "spec_minors_integrated": False,
    "plan_minors_integrated": False,
    "review_scope": "plan",
    "review_base": "",
    "coverage_settled": False,
    "integration_fork": "",
    "plan_index": 1,
    "plan_total": 1,
    "task_total": 0,
}


def test_imp_flat_is_bundled_and_loads() -> None:
    assert "imp-flat" in bundled_workflow_names()
    wf = _flat()
    assert wf.name == "feature"
    assert [p.name for p in wf.phases] == [
        "front-door", "spec", "plan", "implement", "integrate"]


def test_imp_flat_implement_phase_is_flattened() -> None:
    wf = _flat()
    impl = next(p for p in wf.phases if p.name == "implement")
    assert [s.name for s in impl.steps] == [
        "coder", "verify", "cleanup-implement", "autosquash-resolve-implement",
        "code-reviewer", "review-base-repin", "gap-check", "summarizer", "finalize",
    ]


def test_imp_flat_dropped_steps_absent() -> None:
    wf = _flat()
    names = {s.name for s in wf.flat_steps()}
    for gone in ("task-lead", "plan-cycle-check", "deferred-sweep",
                 "sweep-verify", "cleanup-sweep", "autosquash-resolve-sweep"):
        assert gone not in names, gone


def test_imp_flat_dropped_agent_files_absent_and_renames_present() -> None:
    on_disk = {p.stem for p in AGENTS.glob("*.md")}
    for gone in ("task-lead", "deferred-sweep", "plan-code-reviewer", "plan-cycle-check"):
        assert gone not in on_disk, gone
    assert "code-reviewer" in on_disk
    assert "gap-check" in on_disk
    assert "coder" in on_disk


def test_imp_flat_no_orphan_agent_files() -> None:
    wf = _flat()
    referenced = {s.file.stem for p in wf.phases for s in p.steps
                  if s.kind == "agent" and s.file}
    referenced |= {name for a in wf.agents.values() for name in a.subagents}
    on_disk = {p.stem for p in AGENTS.glob("*.md")}
    assert on_disk == referenced, (
        f"orphan files: {on_disk - referenced}; missing: {referenced - on_disk}")


def test_imp_flat_coder_is_high_tier_and_owns_verify_toleration() -> None:
    wf = _flat()
    by = _steps(wf)
    assert by["coder"].tier == "high"
    assert "verify_tolerated" in wf.agents["coder"].output_schema.field_names()
    # tolerated -> skip the verify gate, straight to cleanup
    m = evaluate_rules(by["coder"].rules, vars=_FLAT_VARS,
                       output={"status": "Success", "verify_tolerated": True,
                               "summary": "tolerated"})
    assert isinstance(m.action, Goto) and m.action.target == "cleanup-implement"
    # normal return -> verify gate
    m2 = evaluate_rules(by["coder"].rules, vars=_FLAT_VARS,
                        output={"status": "Success", "verify_tolerated": False,
                                "summary": "done"})
    assert isinstance(m2.action, Continue)


def test_imp_flat_verify_resumes_coder_and_pauses_on_timeout() -> None:
    rules = _steps(_flat())["verify"].rules
    red = evaluate_rules(rules, vars=_FLAT_VARS,
                         output={"exit": 1, "stdout": "", "stderr": ""})
    assert isinstance(red.action, Goto)
    assert red.action.target == "coder" and red.action.resume is True
    timed = evaluate_rules(rules, vars=_FLAT_VARS,
                           output={"exit": 124, "stdout": "", "stderr": ""})
    assert isinstance(timed.action, Pause)
    assert _steps(_flat())["verify"].max_loops == 5


def test_imp_flat_code_reviewer_is_high_tier_and_routes_task_gap_to_coder() -> None:
    wf = _flat()
    by = _steps(wf)
    assert by["code-reviewer"].tier == "high"
    m = evaluate_rules(by["code-reviewer"].rules, vars=_FLAT_VARS,
                       output={"status": "Success", "verdict": "task_gap",
                               "findings": "x"})
    assert isinstance(m.action, Goto)
    assert m.action.target == "coder" and m.action.resume is False
    m2 = evaluate_rules(by["code-reviewer"].rules, vars=_FLAT_VARS,
                        output={"status": "Success", "verdict": "approve",
                                "findings": ""})
    assert isinstance(m2.action, Continue)


def test_imp_flat_cleanup_implement_routes_to_code_reviewer() -> None:
    rules = _steps(_flat())["cleanup-implement"].rules
    m = evaluate_rules(rules, vars=_FLAT_VARS,
                       output={"exit": 0, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "code-reviewer"
    dirty = evaluate_rules(rules, vars=_FLAT_VARS,
                           output={"exit": 3, "stdout": "", "stderr": ""})
    assert isinstance(dirty.action, Goto)
    assert dirty.action.target == "coder" and dirty.action.resume is True


def test_imp_flat_plan_finalized_routes_to_coder() -> None:
    rules = _steps(_flat())["plan-finalized"].rules
    m = evaluate_rules(rules, vars=_FLAT_VARS,
                       output={"status": "Success", "task_total": 3})
    assert isinstance(m.action, Goto) and m.action.target == "coder"


def test_imp_flat_review_base_repin_routing() -> None:
    rules = _steps(_flat())["review-base-repin"].rules
    out = {"exit": 0, "stdout": "abc", "stderr": ""}
    # more outline plans remain -> next plan-writer
    mid = evaluate_rules(
        rules, vars={**_FLAT_VARS, "plan_index": 1, "plan_total": 3}, output=out)
    assert isinstance(mid.action, Goto) and mid.action.target == "plan-writer"
    assert mid.var_mutations["plan_index"] == 2
    # last outline plan implemented -> end-of-change gap-check
    last = evaluate_rules(
        rules, vars={**_FLAT_VARS, "plan_index": 3, "plan_total": 3}, output=out)
    assert isinstance(last.action, Goto) and last.action.target == "gap-check"
    # the change-wide review just ran -> summarizer
    chg = evaluate_rules(
        rules, vars={**_FLAT_VARS, "review_scope": "change"}, output=out)
    assert isinstance(chg.action, Goto) and chg.action.target == "summarizer"
    # a post-settlement corrective (coverage already latched) -> summarizer, per-plan
    corr = evaluate_rules(
        rules, vars={**_FLAT_VARS, "coverage_settled": True, "plan_index": 4,
                     "plan_total": 4}, output=out)
    assert isinstance(corr.action, Goto) and corr.action.target == "summarizer"


def test_imp_flat_gap_check_routing_and_latch() -> None:
    rules = _steps(_flat())["gap-check"].rules
    # gap found -> author corrective via plan-writer (latch stays false)
    gap = evaluate_rules(
        rules, vars={**_FLAT_VARS, "plan_index": 2, "plan_total": 2},
        output={"status": "Success", "more_plans": True, "plan_total": 3,
                "rationale": "x"})
    assert isinstance(gap.action, Goto) and gap.action.target == "plan-writer"
    assert gap.var_mutations["plan_index"] == 3
    assert "coverage_settled" not in gap.var_mutations
    # clean + multi-plan -> latch + change-wide review
    multi = evaluate_rules(
        rules, vars={**_FLAT_VARS, "plan_total": 2},
        output={"status": "Success", "more_plans": False, "plan_total": 2,
                "rationale": "done"})
    assert isinstance(multi.action, Goto) and multi.action.target == "code-reviewer"
    assert multi.var_mutations["coverage_settled"] is True
    assert multi.var_mutations["review_scope"] == "change"
    # clean + single-plan -> latch + skip change-wide review (straight to summarizer)
    single = evaluate_rules(
        rules, vars={**_FLAT_VARS, "plan_total": 1},
        output={"status": "Success", "more_plans": False, "plan_total": 1,
                "rationale": "done"})
    assert isinstance(single.action, Goto) and single.action.target == "summarizer"
    assert single.var_mutations["coverage_settled"] is True


def test_imp_flat_coverage_settled_var_replaces_change_panel_done() -> None:
    wf = _flat()
    d = wf.vars.defaults()
    assert d["coverage_settled"] is False
    assert "change_panel_done" not in d
    body = (FLAT / "workflow.yaml").read_text()
    assert "change_panel_done" not in body


def test_imp_flat_workflow_yaml_drops_removed_identifiers() -> None:
    body = (FLAT / "workflow.yaml").read_text()
    for gone in ("plan-cycle-check", "task-lead", "deferred-sweep",
                 "plan-code-reviewer", "change_panel_done"):
        assert gone not in body, gone


def test_imp_flat_finalize_rework_resets_review_scope_to_plan() -> None:
    rules = _steps(_flat())["finalize"].rules
    base = {**_FLAT_VARS, "review_scope": "change"}
    m = evaluate_rules(rules, vars=base,
                       output={"status": "Success", "decision": "rework",
                               "comments": "", "verify_status": "",
                               "open_items": "", "next_steps": ""})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.var_mutations["review_scope"] == "plan"


def test_imp_flat_coder_and_code_reviewer_are_direct_steps_not_subagents() -> None:
    from imp.engine.loader import BASE_CAPS, TOP_LEVEL_CAPS
    wf = _flat()
    top_leaf = BASE_CAPS | TOP_LEVEL_CAPS
    assert wf.agents["coder"].tools == top_leaf
    assert wf.agents["code-reviewer"].tools == top_leaf


def test_imp_flat_header_states_fresh_context_not_tier_gap() -> None:
    body = (FLAT / "workflow.yaml").read_text()
    low = body.lower()
    assert "fresh context" in low
    assert "adversarial role" in low
    assert "medium-tier" not in low  # the stale tier-gap framing is gone


def _read_agent(name: str) -> str:
    return (AGENTS / name).read_text()


def test_imp_flat_coder_output_schema_is_status_tolerated_summary() -> None:
    fields = set(_flat().agents["coder"].output_schema.field_names())
    assert fields == {"status", "verify_tolerated", "summary"}


def test_imp_flat_coder_documents_verify_triage() -> None:
    text = _read_agent("coder.md")
    low = text.lower()
    assert "DEFERRED verify:" in text
    assert "## Deferred Followups" in text
    assert "— needs-decision:" in text      # em dash U+2014
    assert "verify_tolerated" in text
    assert "attributable" in low
    assert "review_base..HEAD" in text
    assert "non-converging" in low
    assert "max_loops" in text


def test_imp_flat_coder_reads_plan_and_is_whole_plan() -> None:
    low = _read_agent("coder.md").lower()
    assert "whole plan" in low or "entire plan" in low
    assert "read the plan" in low or "reads the plan" in low
    # The old per-task subagent framing is gone.
    assert "task-lead" not in low
    assert "the task-lead's dispatch message" not in low


def test_imp_flat_coder_opportunistic_and_no_fixup_outside_change() -> None:
    text = _read_agent("coder.md")
    low = text.lower()
    assert "opportunistic" in low
    assert "standalone commit" in low
    assert "does not own" in low          # the no-fixup-outside-the-change guardrail
    assert "surviving directive" in low
    assert "forward" in low


def test_imp_flat_coder_sources_human_decision_bar() -> None:
    text = _read_agent("coder.md")
    assert text.count('{% include "partials/human-decision.md" %}') == 1
    assert "DEFERRED coder:" in text


def test_imp_flat_coder_terminal_contract_no_report_back() -> None:
    text = _read_agent("coder.md")
    assert "complete_step" in text
    assert "report back" not in text.lower()
