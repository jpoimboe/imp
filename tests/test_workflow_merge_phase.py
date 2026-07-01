from pathlib import Path

from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

WF = Path("src/imp/assets/workflows/imp").resolve()


def _phase(wf, name):
    return next(p for p in wf.phases if p.name == name)


def _rules(wf, name):
    return next(s for s in wf.flat_steps() if s.name == name).rules


def test_merge_phase_layers_reconciler_loop_on_cleanup_gate() -> None:
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "integrate").steps]
    assert names == [
        "wait-for-head", "integrate-rebase", "reconciler",
        "integration-verify", "integration-reviewer",
        "summarizer", "finalize",
        "cleanup-merge", "autosquash-resolve-merge",
        "assert-stamped", "merge-to-base",
    ]
    by = {s.name: s for s in _phase(wf, "integrate").steps}
    assert by["wait-for-head"].acquire == "base"
    assert by["merge-to-base"].kind == "merge_and_exit_worktree"
    assert all(s.name != "integration-resolver" for s in wf.flat_steps())


def test_integrate_rebase_routing_into_summarizer_and_gate() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integrate-rebase")
    m = evaluate_rules(rules, vars={}, output={"exit": 2, "stdout": ""})
    assert isinstance(m.action, Goto) and m.action.target == "summarizer"
    assert m.var_mutations == {}
    m = evaluate_rules(rules, vars={}, output={"exit": 0, "stdout": "forkSHA"})
    assert isinstance(m.action, Goto) and m.action.target == "integration-verify"
    assert "review_base" not in m.var_mutations
    assert m.var_mutations.get("integration_fork") == "forkSHA"
    m = evaluate_rules(rules, vars={}, output={"exit": 6, "stdout": "forkSHA"})
    assert isinstance(m.action, Continue)
    assert "review_base" not in m.var_mutations
    assert m.var_mutations.get("integration_fork") == "forkSHA"
    m = evaluate_rules(rules, vars={}, output={"exit": 5, "stdout": ""})
    assert isinstance(m.action, Pause)


def test_reconciler_stuck_pauses_and_else_continues() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "reconciler")
    m = evaluate_rules(rules, vars={},
                       output={"status": "Success", "stuck": True, "summary": ""})
    assert isinstance(m.action, Pause)
    m = evaluate_rules(rules, vars={},
                       output={"status": "Success", "stuck": False, "summary": ""})
    assert isinstance(m.action, Continue)


def test_only_finalize_routes_to_planning_and_releases_the_lane() -> None:
    wf = load_workflow(WF)
    planning = {s.name for p in wf.phases if p.name in ("spec", "plan", "implement")
                for s in p.steps}
    for step in _phase(wf, "integrate").steps:
        for rule in step.rules:
            a = rule.action
            target = getattr(a, "target", None)
            release = getattr(a, "release", None)
            if step.name == "finalize" and target == "plan-writer":
                assert release == "base", (step.name, a)
                continue
            assert target not in planning, (step.name, a)
            assert release is None, (step.name, a)


def test_integration_verify_routes_failure_to_integration_reviewer() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integration-verify")
    m = evaluate_rules(rules, vars={}, output={"exit": 0})
    assert isinstance(m.action, Continue)
    m = evaluate_rules(rules, vars={}, output={"exit": 124})
    assert isinstance(m.action, Pause)
    m = evaluate_rules(rules, vars={}, output={"exit": 1})
    assert isinstance(m.action, Goto) and m.action.target == "integration-reviewer"
    assert m.action.release is None


def test_integration_review_routes_blocking_and_minor_to_reconciler() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integration-reviewer")
    base = {"status": "Success", "findings": ""}
    m = evaluate_rules(rules, vars={},
                       output={**base, "blocking": True, "has_minor_findings": False})
    assert isinstance(m.action, Goto) and m.action.target == "reconciler"
    assert m.action.resume is True
    m = evaluate_rules(rules, vars={},
                       output={**base, "blocking": False, "has_minor_findings": True})
    assert isinstance(m.action, Goto) and m.action.target == "reconciler"
    assert m.action.resume is True
    m = evaluate_rules(rules, vars={},
                       output={**base, "blocking": False, "has_minor_findings": False})
    # both False -> clean, proceed to the human gate
    assert isinstance(m.action, Goto) and m.action.target == "summarizer"
    m = evaluate_rules(rules, vars={},
                       output={"status": "Error", "findings": "",
                               "blocking": False, "has_minor_findings": False})
    assert isinstance(m.action, Pause)


def test_cleanup_merge_routes_by_exit_code() -> None:
    wf = load_workflow(WF)
    step = next(s for s in wf.flat_steps() if s.name == "cleanup-merge")
    assert step.kind == "script"
    assert step.command == "./scripts/cleanup.sh"
    r = step.rules
    m0 = evaluate_rules(r, vars={}, output={"exit": 0})
    assert isinstance(m0.action, Goto) and m0.action.target == "assert-stamped"
    m2 = evaluate_rules(r, vars={}, output={"exit": 2})
    assert isinstance(m2.action, Continue)
    m3 = evaluate_rules(r, vars={}, output={"exit": 3})
    assert isinstance(m3.action, Goto) and m3.action.target == "reconciler"
    assert m3.action.resume is True
    m1 = evaluate_rules(r, vars={}, output={"exit": 1})
    assert isinstance(m1.action, Pause)
    resolver = next(s for s in wf.flat_steps() if s.name == "autosquash-resolve-merge")
    assert resolver.kind == "agent" and resolver.tier == "high"
    assert resolver.file is not None and resolver.file.name == "autosquash-resolve.md"
    assert resolver.rules[-1].action.target == "cleanup-merge"
    assert resolver.max_loops == 6


def test_assert_stamped_is_readonly_gate_before_merge() -> None:
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "integrate").steps]
    assert names.index("assert-stamped") == names.index("merge-to-base") - 1
    step = next(s for s in wf.flat_steps() if s.name == "assert-stamped")
    assert step.kind == "script"
    assert step.command == "./scripts/assert-stamped.sh"
    m0 = evaluate_rules(step.rules, vars={}, output={"exit": 0})
    assert isinstance(m0.action, Continue)
    m1 = evaluate_rules(step.rules, vars={}, output={"exit": 1})
    assert isinstance(m1.action, Pause)


def test_merge_to_base_recovery_rules_and_cap() -> None:
    wf = load_workflow(WF)
    step = next(s for s in wf.flat_steps() if s.name == "merge-to-base")
    assert step.max_loops == 3
    rules = step.rules

    m = evaluate_rules(rules, vars={}, output={"merge": "MERGED"})
    assert isinstance(m.action, Continue)

    m = evaluate_rules(rules, vars={}, output={"merge": "DIRTY"})
    assert isinstance(m.action, Goto) and m.action.target == "reconciler"
    assert m.action.release is None
    assert m.action.feedback is not None and "uncommitted" in m.action.feedback
    assert m.rule.label == "recovering merge"

    m = evaluate_rules(rules, vars={}, output={"merge": "CONFLICT"})
    assert isinstance(m.action, Goto) and m.action.target == "integrate-rebase"
    assert m.action.release is None
    assert m.rule.label == "retrying integration"

    m = evaluate_rules(rules, vars={}, output={"merge": "REFUSED"})
    assert isinstance(m.action, Pause)

    m = evaluate_rules(rules, vars={}, output={"merge": "SOMETHING_ELSE"})
    assert isinstance(m.action, Pause)


def test_finalize_merge_routes_to_cleanup_merge() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "finalize")
    m = evaluate_rules(rules, vars={"plan_index": 1, "plan_total": 1},
                       output={"decision": "merge"})
    assert isinstance(m.action, Goto) and m.action.target == "cleanup-merge"
    m = evaluate_rules(rules, vars={"plan_index": 1, "plan_total": 1},
                       output={"decision": "rework"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    assert m.action.release == "base"
    m = evaluate_rules(rules, vars={"plan_index": 1, "plan_total": 1},
                       output={"decision": "other"})
    assert isinstance(m.action, Pause)
