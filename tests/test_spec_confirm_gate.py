"""Rule-path unit tests for the spec confirm gate (design §Feature 1).

Every test loads the SHIPPED default workflow and drives its real rules through
evaluate_rules — no hand-built rule fixtures — so a yaml edit that breaks a rule
path fails here.
"""
from __future__ import annotations

from imp.assets import default_workflow_dir
from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto


def _wf():
    return load_workflow(default_workflow_dir())


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


# All declared workflow vars at their defaults — a realistic evaluate_rules env.
_BASE_VARS = {
    "ready_to_merge": False, "final_review": True, "fast_path": False,
    "spec_minors_integrated": False, "plan_minors_integrated": False,
    "panel_done": False, "integration_needs_review": False,
    "spec_confirm": True, "spec_unseen_changes": False,
    "spec_gate_pending": False, "spec_gate_passed": False,
}


def test_new_gate_vars_declared_with_design_defaults() -> None:
    defaults = _wf().vars.defaults()
    assert defaults["spec_confirm"] is True
    assert defaults["spec_unseen_changes"] is False
    assert defaults["spec_gate_pending"] is False
    assert defaults["spec_gate_passed"] is False


def test_spec_writer_gated_confirm_sets_passed_and_continues() -> None:
    rules = _steps(_wf())["spec-writer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_gate_pending": True},
        output={"status": "Success", "phase": "confirmed",
                "complexity": "standard", "unseen_changes": False},
    )
    assert isinstance(m.action, Continue)
    assert m.var_mutations["spec_gate_pending"] is False
    assert m.var_mutations["spec_gate_passed"] is True
    # Rule 1 is taken exclusively: it must NOT fall through to the else branch,
    # which would also clear fast_path and arm spec_unseen_changes.
    assert "fast_path" not in m.var_mutations
    assert "spec_unseen_changes" not in m.var_mutations


def test_spec_writer_else_consumes_pending_and_clears_passed_and_arms() -> None:
    # A `written` rework that changed the spec undirected: arm the var, and
    # one-shot-clear both guard flags on the else branch.
    rules = _steps(_wf())["spec-writer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_gate_pending": True,
              "spec_gate_passed": True, "spec_unseen_changes": False},
        output={"status": "Success", "phase": "written",
                "complexity": "standard", "unseen_changes": True},
    )
    assert isinstance(m.action, Continue)
    assert m.var_mutations["spec_gate_pending"] is False
    assert m.var_mutations["spec_gate_passed"] is False
    assert m.var_mutations["spec_unseen_changes"] is True
    assert m.var_mutations["fast_path"] is False


def test_spec_writer_confirmed_with_unseen_true_falls_to_else() -> None:
    # A confirmed completion self-reporting undirected edits is a declared
    # contract violation: it must NOT take the gated-confirm branch.
    rules = _steps(_wf())["spec-writer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_gate_pending": True},
        output={"status": "Success", "phase": "confirmed",
                "complexity": "standard", "unseen_changes": True},
    )
    assert isinstance(m.action, Continue)
    assert m.var_mutations["spec_gate_pending"] is False
    assert m.var_mutations["spec_gate_passed"] is False
    assert m.var_mutations["spec_unseen_changes"] is True


def test_spec_writer_first_pass_written_does_not_arm() -> None:
    # Initial written turn reports unseen_changes:false -> var stays false,
    # fast_path tracks complexity.
    rules = _steps(_wf())["spec-writer"].rules
    m = evaluate_rules(
        rules, vars=_BASE_VARS,
        output={"status": "Success", "phase": "written",
                "complexity": "trivial", "unseen_changes": False},
    )
    assert m.var_mutations["spec_unseen_changes"] is False
    assert m.var_mutations["fast_path"] is True


def test_spec_verify_commit_failure_clears_gate_passed() -> None:
    rules = _steps(_wf())["spec-verify-commit"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_gate_passed": True},
        output={"exit": 1},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-writer" and m.action.resume is True
    assert m.var_mutations["spec_gate_passed"] is False


def test_spec_verify_commit_success_continues_no_mutation() -> None:
    rules = _steps(_wf())["spec-verify-commit"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS, output={"exit": 0})
    assert isinstance(m.action, Continue)
    assert m.var_mutations == {}


def test_spec_exit_gate_routes_gate_passed_to_plan_and_clears_flag() -> None:
    rules = _steps(_wf())["spec-exit-gate"].rules
    m = evaluate_rules(rules, vars={**_BASE_VARS, "spec_gate_passed": True},
                       output=None)
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is False
    assert m.var_mutations["spec_gate_passed"] is False


def test_spec_exit_gate_fast_path_clears_unseen_and_skips_to_plan() -> None:
    rules = _steps(_wf())["spec-exit-gate"].rules
    m = evaluate_rules(rules, vars={**_BASE_VARS, "fast_path": True,
                                    "spec_unseen_changes": True}, output=None)
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer"
    assert m.var_mutations["spec_unseen_changes"] is False


def test_spec_exit_gate_else_continues_into_review() -> None:
    rules = _steps(_wf())["spec-exit-gate"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS, output=None)
    assert isinstance(m.action, Continue)


_CONFIRM_FB = ("The review cycle is complete and the spec is accepted. "
               "Surface any significant decisions made during review to the "
               "user and confirm before finalizing:")


def test_reviewer_rule2_armed_clean_after_minors_fires_gate() -> None:
    rules = _steps(_wf())["spec-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": True,
              "spec_confirm": True, "spec_unseen_changes": True},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-writer" and m.action.resume is True
    assert m.action.feedback == _CONFIRM_FB
    assert m.var_mutations["spec_gate_pending"] is True
    assert m.var_mutations["spec_minors_integrated"] is False
    assert m.var_mutations["spec_unseen_changes"] is False


def test_reviewer_rule3_armed_but_disabled_clears_and_proceeds() -> None:
    # gate disabled (spec_confirm:false) but armed -> proceed, vars cleared.
    rules = _steps(_wf())["spec-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": True,
              "spec_confirm": False, "spec_unseen_changes": True},
        output={"blocking": False, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is False
    assert m.var_mutations["spec_minors_integrated"] is False
    assert m.var_mutations["spec_unseen_changes"] is False


def test_reviewer_rule5_armed_clean_no_minors_fires_gate() -> None:
    rules = _steps(_wf())["spec-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": False,
              "spec_confirm": True, "spec_unseen_changes": True},
        output={"blocking": False, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-writer" and m.action.resume is True
    assert m.action.feedback == _CONFIRM_FB
    assert m.var_mutations["spec_gate_pending"] is True
    assert m.var_mutations["spec_unseen_changes"] is False


def test_reviewer_rule6_armed_but_disabled_clears_on_no_minors_path() -> None:
    rules = _steps(_wf())["spec-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_confirm": False, "spec_unseen_changes": True},
        output={"blocking": False, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer"
    assert m.var_mutations["spec_unseen_changes"] is False


# ── precedence (first-match-wins is load-bearing) ──
def test_reviewer_rule1_beats_2_and_3_on_blocking_with_minors_integrated() -> None:
    rules = _steps(_wf())["spec-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": True,
              "spec_unseen_changes": True},
        output={"blocking": True, "has_minor_findings": False},
    )
    assert m.action.target == "spec-writer" and m.action.resume is True
    assert m.action.feedback == "Here's review feedback to address:"
    assert m.var_mutations["spec_minors_integrated"] is False


def test_reviewer_rule2_beats_4_verification_with_new_minors_while_armed() -> None:
    # armed verification review still carrying fresh minors -> gate fires (rule 2),
    # the new minors are dropped (NOT a second minors pass).
    rules = _steps(_wf())["spec-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": True,
              "spec_confirm": True, "spec_unseen_changes": True},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.action.feedback == _CONFIRM_FB
    assert m.var_mutations["spec_gate_pending"] is True


def test_reviewer_rule4_beats_5_clean_minors_armed_not_yet_integrated() -> None:
    # clean + actionable minors + armed but minors not yet integrated -> minors
    # pass FIRST (rule 4); the gate fires only after the verification review.
    rules = _steps(_wf())["spec-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_minors_integrated": False,
              "spec_confirm": True, "spec_unseen_changes": True},
        output={"blocking": False, "has_minor_findings": True},
    )
    assert m.action.target == "spec-writer" and m.action.resume is True
    assert m.action.feedback == ("No blocking issues — integrate these minor "
                                 "improvements; a final review follows:")
    assert m.var_mutations["spec_minors_integrated"] is True


def _apply(state: dict, muts: dict) -> dict:
    out = dict(state)
    out.update(muts)
    return out


def test_trace_criterion2_rework_then_clean_reaches_gated_planning() -> None:
    wf = _wf()
    rules = _steps(wf)
    v = dict(_BASE_VARS)

    # 1) reviewer blocking -> rework
    m = evaluate_rules(rules["spec-reviewer"].rules, vars=v,
                       output={"blocking": True, "has_minor_findings": False})
    v = _apply(v, m.var_mutations)
    assert m.action.target == "spec-writer" and m.action.resume is True

    # 2) writer reworks, undirected edits -> arms via else rule
    m = evaluate_rules(rules["spec-writer"].rules, vars=v,
                       output={"status": "Success", "phase": "written",
                               "complexity": "standard", "unseen_changes": True})
    v = _apply(v, m.var_mutations)
    assert v["spec_unseen_changes"] is True

    # 3) verify-commit ok -> continue (exit-gate else -> continue) -> re-review
    m = evaluate_rules(rules["spec-verify-commit"].rules, vars=v, output={"exit": 0})
    v = _apply(v, m.var_mutations)
    m = evaluate_rules(rules["spec-exit-gate"].rules, vars=v, output=None)
    v = _apply(v, m.var_mutations)
    assert isinstance(m.action, Continue)

    # 4) clean re-review, armed, no minors -> fires the gate (rule 5)
    m = evaluate_rules(rules["spec-reviewer"].rules, vars=v,
                       output={"blocking": False, "has_minor_findings": False})
    v = _apply(v, m.var_mutations)
    assert v["spec_gate_pending"] is True and v["spec_unseen_changes"] is False
    assert m.action.target == "spec-writer" and m.action.resume is True

    # 5) gated confirm, no further edits -> sets spec_gate_passed, continues
    m = evaluate_rules(rules["spec-writer"].rules, vars=v,
                       output={"status": "Success", "phase": "confirmed",
                               "complexity": "standard", "unseen_changes": False})
    v = _apply(v, m.var_mutations)
    assert v["spec_gate_passed"] is True and v["spec_gate_pending"] is False
    assert isinstance(m.action, Continue)

    # 6) verify-commit ok -> exit-gate routes spec_gate_passed to plan, clears flag
    m = evaluate_rules(rules["spec-verify-commit"].rules, vars=v, output={"exit": 0})
    v = _apply(v, m.var_mutations)
    m = evaluate_rules(rules["spec-exit-gate"].rules, vars=v, output=None)
    v = _apply(v, m.var_mutations)
    assert m.action.target == "plan-writer" and m.action.resume is False
    assert v["spec_gate_passed"] is False


def test_trace_criterion3_postgate_edit_clean_rereview_no_second_confirm() -> None:
    wf = _wf()
    rules = _steps(wf)
    # Enter the confirm turn already granted; user requests an edit at the gate.
    v = {**_BASE_VARS, "spec_gate_pending": True}
    # writer emits written + unseen_changes:false (user-directed edit) -> else rule
    m = evaluate_rules(rules["spec-writer"].rules, vars=v,
                       output={"status": "Success", "phase": "written",
                               "complexity": "standard", "unseen_changes": False})
    v = _apply(v, m.var_mutations)
    assert v["spec_gate_pending"] is False and v["spec_unseen_changes"] is False
    # verify ok -> exit-gate else -> continue -> clean re-review, NOT armed -> rule 6
    m = evaluate_rules(rules["spec-verify-commit"].rules, vars=v, output={"exit": 0})
    v = _apply(v, m.var_mutations)
    m = evaluate_rules(rules["spec-exit-gate"].rules, vars=v, output=None)
    v = _apply(v, m.var_mutations)
    assert isinstance(m.action, Continue)
    m = evaluate_rules(rules["spec-reviewer"].rules, vars=v,
                       output={"blocking": False, "has_minor_findings": False})
    v = _apply(v, m.var_mutations)
    assert m.action.target == "plan-writer"          # no second confirm
    assert v.get("spec_gate_pending", False) is False


def test_trace_criterion5_gated_confirm_verifyfail_recovery_rereviews() -> None:
    wf = _wf()
    rules = _steps(wf)
    # gated confirm that committed mis-staged edits: writer rule sets passed+continue
    v = {**_BASE_VARS, "spec_gate_pending": True}
    m = evaluate_rules(rules["spec-writer"].rules, vars=v,
                       output={"status": "Success", "phase": "confirmed",
                               "complexity": "standard", "unseen_changes": False})
    v = _apply(v, m.var_mutations)
    assert v["spec_gate_passed"] is True
    # verify-commit FAILS (uncommitted spec edit) -> clears spec_gate_passed, resume
    m = evaluate_rules(rules["spec-verify-commit"].rules, vars=v, output={"exit": 1})
    v = _apply(v, m.var_mutations)
    assert v["spec_gate_passed"] is False
    assert m.action.target == "spec-writer" and m.action.resume is True
    # recovery turn re-emits written -> else rule clears passed again, arms per report
    m = evaluate_rules(rules["spec-writer"].rules, vars=v,
                       output={"status": "Success", "phase": "written",
                               "complexity": "standard", "unseen_changes": False})
    v = _apply(v, m.var_mutations)
    assert v["spec_gate_passed"] is False
    # verify ok -> exit-gate: spec_gate_passed false, not fast_path -> else continue
    m = evaluate_rules(rules["spec-verify-commit"].rules, vars=v, output={"exit": 0})
    v = _apply(v, m.var_mutations)
    m = evaluate_rules(rules["spec-exit-gate"].rules, vars=v, output=None)
    v = _apply(v, m.var_mutations)
    assert isinstance(m.action, Continue)   # the stale grant reached neither exit nor planning
