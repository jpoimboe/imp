from pathlib import Path

from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

WF = Path("src/imp/assets/workflows/imp").resolve()


def _phase(wf, name):
    return next(p for p in wf.phases if p.name == name)


def test_finalize_tails_the_implement_phase() -> None:
    wf = load_workflow(WF)
    impl = [s.name for s in _phase(wf, "implement").steps]
    assert impl[-2:] == ["review-base-repin", "finalize"]
    assert "stamp-trailers" not in impl
    assert not any(p.name == "review" for p in wf.phases)


def test_merge_phase_linear_queue_flow() -> None:
    wf = load_workflow(WF)
    steps = _phase(wf, "merge").steps
    names = [s.name for s in steps]
    assert names == [
        "wait-for-head", "integrate-rebase", "integrator", "integration-reviewer",
        "integration-verify", "cleanup-merge", "autosquash-resolve-merge",
        "integration-resolver", "assert-stamped", "merge-to-base",

    ]
    by = {s.name: s for s in steps}
    assert by["wait-for-head"].kind == "branch"
    assert by["wait-for-head"].acquire == "base"
    assert by["integration-resolver"].release == "base"
    assert by["merge-to-base"].kind == "merge_and_exit_worktree"


def test_optimistic_concurrency_vars_and_step_gone() -> None:
    wf = load_workflow(WF)
    var_names = wf.vars.names()
    assert "merge_approved" not in var_names
    assert "reintegrate_clean" not in var_names
    assert all(s.name != "base-fresh-check" for s in wf.flat_steps())


def test_integrate_rebase_routing() -> None:
    wf = load_workflow(WF)
    rules = next(s for s in wf.flat_steps() if s.name == "integrate-rebase").rules
    _BASE = {}
    # exit 2 -> base unmoved, merge directly via the pre-merge backstop
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 2})
    assert isinstance(m.action, Goto) and m.action.target == "assert-stamped"
    # exit 0 -> semantic review
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 0})
    assert isinstance(m.action, Goto) and m.action.target == "integration-reviewer"
    # exit 6 -> integrator (continue)
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 6})
    assert isinstance(m.action, Continue)
    # else (e.g., exit 5) -> pause
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 5})
    assert isinstance(m.action, Pause)


def test_integration_review_routing() -> None:
    wf = load_workflow(WF)
    rules = next(s for s in wf.flat_steps() if s.name == "integration-reviewer").rules
    _BASE = {}
    out = {"verdict": "breakage", "status": "Success", "summary": ""}
    m = evaluate_rules(rules, vars=_BASE, output=out)
    assert isinstance(m.action, Goto) and m.action.target == "integration-resolver"
    out = {"verdict": "ok", "status": "Success", "summary": ""}
    m = evaluate_rules(rules, vars=_BASE, output=out)
    assert isinstance(m.action, Continue)


def test_integration_verify_routing() -> None:
    wf = load_workflow(WF)
    rules = next(s for s in wf.flat_steps() if s.name == "integration-verify").rules
    _BASE = {}
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 0})
    assert isinstance(m.action, Continue)        # -> cleanup-merge (the next step)
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 124})
    assert isinstance(m.action, Pause)
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 1})
    assert isinstance(m.action, Goto) and m.action.target == "integration-resolver"


def test_integration_resolve_routing() -> None:
    wf = load_workflow(WF)
    rules = next(s for s in wf.flat_steps() if s.name == "integration-resolver").rules
    _BASE = {}
    base = {"status": "Success", "comments": ""}
    m = evaluate_rules(rules, vars=_BASE, output={**base, "decision": "merge"})
    assert isinstance(m.action, Goto) and m.action.target == "wait-for-head"
    m = evaluate_rules(rules, vars=_BASE, output={**base, "decision": "rework"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    m = evaluate_rules(rules, vars=_BASE, output={**base, "decision": "pause"})
    assert isinstance(m.action, Pause)


def test_finalize_merge_routes_to_wait_for_head() -> None:
    wf = load_workflow(WF)
    rules = next(s for s in wf.flat_steps() if s.name == "finalize").rules
    _BASE = {}
    base = {"status": "Success", "comments": "", "built": "", "verify_status": "",
            "open_items": "", "next_steps": ""}
    m = evaluate_rules(rules, vars=_BASE, output={**base, "decision": "merge"})
    assert isinstance(m.action, Goto) and m.action.target == "wait-for-head"


def test_finalize_rework_and_pause_routing() -> None:
    wf = load_workflow(WF)
    rules = next(s for s in wf.flat_steps() if s.name == "finalize").rules
    _BASE = {}
    base = {"status": "Success", "comments": "", "built": "", "verify_status": "",
            "open_items": "", "next_steps": ""}
    m = evaluate_rules(rules, vars=_BASE, output={**base, "decision": "rework"})
    assert isinstance(m.action, Goto) and m.action.target == "plan-writer"
    m = evaluate_rules(rules, vars=_BASE, output={**base, "decision": "other"})
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
    assert isinstance(m2.action, Continue)               # fall through into the resolver
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


def test_integrate_rebase_clears_review_base_on_rewrite() -> None:
    wf = load_workflow(WF)
    rules = next(s for s in wf.flat_steps() if s.name == "integrate-rebase").rules
    m0 = evaluate_rules(rules, vars={}, output={"exit": 0})
    assert m0.var_mutations.get("review_base") == ""     # clean replant clears the pin
    m6 = evaluate_rules(rules, vars={}, output={"exit": 6})
    assert m6.var_mutations.get("review_base") == ""     # conflict-replant clears it too
    m2 = evaluate_rules(rules, vars={}, output={"exit": 2})
    assert "review_base" not in m2.var_mutations          # base unmoved: no rewrite, no clear
