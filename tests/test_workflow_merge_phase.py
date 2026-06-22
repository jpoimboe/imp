from pathlib import Path

from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

WF = Path("src/imp/assets/workflows/imp").resolve()


def _phase(wf, name):
    return next(p for p in wf.phases if p.name == name)


def _rules(wf, name):
    return next(s for s in wf.flat_steps() if s.name == name).rules


def test_merge_phase_layers_integrator_loop_on_cleanup_gate() -> None:
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "merge").steps]
    assert names == [
        "wait-for-head", "integrate-rebase", "integrator",
        "integration-verify", "integration-reviewer",
        "cleanup-merge", "autosquash-resolve-merge",
        "assert-stamped", "merge-to-base",
    ]
    by = {s.name: s for s in _phase(wf, "merge").steps}
    assert by["wait-for-head"].acquire == "base"
    assert by["merge-to-base"].kind == "merge_and_exit_worktree"
    assert all(s.name != "integration-resolver" for s in wf.flat_steps())


def test_integrate_rebase_routing_into_cleanup_gate() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integrate-rebase")
    m = evaluate_rules(rules, vars={}, output={"exit": 2, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "assert-stamped"
    assert "integration_fork" not in m.var_mutations            # exit 2 emits no fork SHA
    m = evaluate_rules(rules, vars={}, output={"exit": 0, "stdout": "forkSHA", "stderr": ""})
    assert isinstance(m.action, Goto) and m.action.target == "integration-verify"
    assert m.var_mutations.get("review_base") == ""
    assert m.var_mutations.get("integration_fork") == "forkSHA"
    m = evaluate_rules(rules, vars={}, output={"exit": 6, "stdout": "forkSHA", "stderr": ""})
    assert isinstance(m.action, Continue)
    assert m.var_mutations.get("review_base") == ""
    assert m.var_mutations.get("integration_fork") == "forkSHA"
    m = evaluate_rules(rules, vars={}, output={"exit": 5, "stdout": "", "stderr": ""})
    assert isinstance(m.action, Pause)


def test_integrator_stuck_pauses_and_else_continues() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integrator")
    m = evaluate_rules(rules, vars={},
                       output={"status": "Success", "stuck": True, "summary": ""})
    assert isinstance(m.action, Pause)
    m = evaluate_rules(rules, vars={},
                       output={"status": "Success", "stuck": False, "summary": ""})
    assert isinstance(m.action, Continue)


def test_merge_phase_never_routes_to_planning_or_releases_the_lane() -> None:
    wf = load_workflow(WF)
    planning = {s.name for p in wf.phases if p.name in ("spec", "plan", "implement")
                for s in p.steps}
    for step in _phase(wf, "merge").steps:
        for rule in step.rules:
            a = rule.action
            assert getattr(a, "target", None) not in planning, (step.name, a)
            assert getattr(a, "release", None) is None, (step.name, a)


def test_integration_verify_routes_failure_to_integrator() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integration-verify")
    m = evaluate_rules(rules, vars={}, output={"exit": 0})
    assert isinstance(m.action, Continue)
    m = evaluate_rules(rules, vars={}, output={"exit": 124})
    assert isinstance(m.action, Pause)
    m = evaluate_rules(rules, vars={}, output={"exit": 1})
    assert isinstance(m.action, Goto) and m.action.target == "integrator"
    assert m.action.resume is True
    assert m.action.release is None


def test_integration_review_routes_blocking_and_minor_to_integrator() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "integration-reviewer")
    base = {"status": "Success", "findings": ""}
    m = evaluate_rules(rules, vars={},
                       output={**base, "blocking": True, "has_minor_findings": False})
    assert isinstance(m.action, Goto) and m.action.target == "integrator"
    assert m.action.resume is True
    m = evaluate_rules(rules, vars={},
                       output={**base, "blocking": False, "has_minor_findings": True})
    assert isinstance(m.action, Goto) and m.action.target == "integrator"
    assert m.action.resume is True
    m = evaluate_rules(rules, vars={},
                       output={**base, "blocking": False, "has_minor_findings": False})
    assert isinstance(m.action, Goto) and m.action.target == "cleanup-merge"
    m = evaluate_rules(rules, vars={},
                       output={"status": "Error", "findings": "",
                               "blocking": False, "has_minor_findings": False})
    assert isinstance(m.action, Pause)


def test_cleanup_merge_routes_by_exit_code() -> None:
    wf = load_workflow(WF)
    step = next(s for s in wf.flat_steps() if s.name == "cleanup-merge")
    assert step.kind == "script"
    assert step.command == './scripts/cleanup.sh "{{ vars.review_base }}"'
    r = step.rules
    m0 = evaluate_rules(r, vars={}, output={"exit": 0})
    assert isinstance(m0.action, Goto) and m0.action.target == "assert-stamped"
    m2 = evaluate_rules(r, vars={}, output={"exit": 2})
    assert isinstance(m2.action, Continue)
    m3 = evaluate_rules(r, vars={}, output={"exit": 3})
    assert isinstance(m3.action, Goto) and m3.action.target == "integrator"
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
    names = [s.name for s in _phase(wf, "merge").steps]
    assert names.index("assert-stamped") == names.index("merge-to-base") - 1
    step = next(s for s in wf.flat_steps() if s.name == "assert-stamped")
    assert step.kind == "script"
    assert step.command == "./scripts/assert-stamped.sh"
    m0 = evaluate_rules(step.rules, vars={}, output={"exit": 0})
    assert isinstance(m0.action, Continue)
    m1 = evaluate_rules(step.rules, vars={}, output={"exit": 1})
    assert isinstance(m1.action, Pause)


def test_finalize_merge_routes_to_wait_for_head() -> None:
    wf = load_workflow(WF)
    rules = _rules(wf, "finalize")
    base = {"status": "Success", "comments": "", "built": "", "verify_status": "",
            "open_items": "", "next_steps": ""}
    m = evaluate_rules(rules, vars={}, output={**base, "decision": "merge"})
    assert isinstance(m.action, Goto) and m.action.target == "wait-for-head"
    m = evaluate_rules(rules, vars={}, output={**base, "decision": "rework"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    m = evaluate_rules(rules, vars={}, output={**base, "decision": "other"})
    assert isinstance(m.action, Pause)
