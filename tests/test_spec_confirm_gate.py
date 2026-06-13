"""Rule-path unit tests for the spec confirm gate (forward spec-confirm step).

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
    "fast_path": False,
    "spec_minors_integrated": False, "plan_minors_integrated": False,
    "panel_done": False,
    "spec_confirm": True, "spec_unseen_changes": False,
}


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


def test_spec_verify_commit_success_continues_no_mutation() -> None:
    rules = _steps(_wf())["spec-verify-commit"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS, output={"exit": 0})
    assert isinstance(m.action, Continue)
    assert m.var_mutations == {}


def test_spec_verify_commit_failure_resumes_spec_writer() -> None:
    rules = _steps(_wf())["spec-verify-commit"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS, output={"exit": 1})
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-writer" and m.action.resume is True
    assert m.var_mutations == {}


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


def test_reviewer_rule4_beats_5_clean_minors_armed_not_yet_integrated() -> None:
    # clean + actionable minors + armed but minors not yet integrated -> minors
    # pass FIRST (rule 2); the confirm gate fires only after the verification review.
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


def test_reviewer_gate_disabled_clears_and_proceeds() -> None:
    # gate disabled (spec_confirm:false) but unseen -> proceed, vars cleared.
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


def test_reviewer_gate_disabled_on_no_minors_path() -> None:
    rules = _steps(_wf())["spec-reviewer"].rules
    m = evaluate_rules(
        rules,
        vars={**_BASE_VARS, "spec_confirm": False, "spec_unseen_changes": True},
        output={"blocking": False, "has_minor_findings": False},
    )
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer"
    assert m.var_mutations["spec_unseen_changes"] is False


def test_spec_confirm_approve_routes_to_plan_writer() -> None:
    rules = _steps(_wf())["spec-confirm"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "confirm": "approve", "comments": ""})
    assert isinstance(m.action, Goto)
    assert m.action.target == "plan-writer" and m.action.resume is False
    assert m.var_mutations == {}


def test_spec_confirm_changes_resumes_spec_writer_and_rearms() -> None:
    rules = _steps(_wf())["spec-confirm"].rules
    m = evaluate_rules(rules, vars=_BASE_VARS,
                       output={"status": "Success", "confirm": "changes", "comments": "x"})
    assert isinstance(m.action, Goto)
    assert m.action.target == "spec-writer" and m.action.resume is True
    assert m.var_mutations["spec_unseen_changes"] is True
    assert m.action.feedback == "The user reviewed the spec and requested changes. Address them:"


def _apply(state: dict, muts: dict) -> dict:
    out = dict(state)
    out.update(muts)
    return out


def test_trace_rework_then_clean_reaches_forward_confirm() -> None:
    wf = _wf()
    rules = _steps(wf)
    v = dict(_BASE_VARS)

    # 1) reviewer blocking -> rework
    m = evaluate_rules(rules["spec-reviewer"].rules, vars=v,
                       output={"blocking": True, "has_minor_findings": False})
    v = _apply(v, m.var_mutations)
    assert m.action.target == "spec-writer" and m.action.resume is True

    # 2) writer reworks, undirected edits -> arms spec_unseen_changes
    m = evaluate_rules(rules["spec-writer"].rules, vars=v,
                       output={"status": "Success", "phase": "written",
                               "complexity": "standard", "unseen_changes": True})
    v = _apply(v, m.var_mutations)
    assert v["spec_unseen_changes"] is True

    # 3) verify-commit ok -> continue -> re-review
    m = evaluate_rules(rules["spec-verify-commit"].rules, vars=v, output={"exit": 0})
    v = _apply(v, m.var_mutations)

    # 4) clean re-review, armed, no minors -> routes to spec-confirm
    m = evaluate_rules(rules["spec-reviewer"].rules, vars=v,
                       output={"blocking": False, "has_minor_findings": False})
    v = _apply(v, m.var_mutations)
    assert m.action.target == "spec-confirm" and m.action.resume is False
    assert v["spec_unseen_changes"] is False

    # 5) user approves -> routes to plan-writer
    m = evaluate_rules(rules["spec-confirm"].rules, vars=v,
                       output={"status": "Success", "confirm": "approve", "comments": ""})
    v = _apply(v, m.var_mutations)
    assert m.action.target == "plan-writer" and m.action.resume is False


def test_trace_confirm_changes_then_rereview_and_approve() -> None:
    wf = _wf()
    rules = _steps(wf)
    # Start: at the confirm step, user requests changes
    v = {**_BASE_VARS, "spec_unseen_changes": False}

    # 1) spec-confirm: user requests changes -> re-arm and resume spec-writer
    m = evaluate_rules(rules["spec-confirm"].rules, vars=v,
                       output={"status": "Success", "confirm": "changes",
                               "comments": "add more detail"})
    v = _apply(v, m.var_mutations)
    assert m.action.target == "spec-writer" and m.action.resume is True
    assert v["spec_unseen_changes"] is True

    # 2) spec-writer applies user-directed edits -> unseen_changes=false (user directed)
    m = evaluate_rules(rules["spec-writer"].rules, vars=v,
                       output={"status": "Success", "phase": "written",
                               "complexity": "standard", "unseen_changes": False})
    v = _apply(v, m.var_mutations)
    # spec_unseen_changes = spec_unseen_changes OR unseen_changes = True OR False = True
    # (the re-arm from spec-confirm is preserved)
    assert v["spec_unseen_changes"] is True

    # 3) verify-commit ok -> continue
    m = evaluate_rules(rules["spec-verify-commit"].rules, vars=v, output={"exit": 0})
    v = _apply(v, m.var_mutations)

    # 4) re-review: armed, clean, no minors -> spec-confirm again
    m = evaluate_rules(rules["spec-reviewer"].rules, vars=v,
                       output={"blocking": False, "has_minor_findings": False})
    v = _apply(v, m.var_mutations)
    assert m.action.target == "spec-confirm" and m.action.resume is False

    # 5) user approves -> plan-writer
    m = evaluate_rules(rules["spec-confirm"].rules, vars=v,
                       output={"status": "Success", "confirm": "approve", "comments": ""})
    v = _apply(v, m.var_mutations)
    assert m.action.target == "plan-writer" and m.action.resume is False
