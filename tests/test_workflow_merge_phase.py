from pathlib import Path

from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

WF = Path("src/imp/assets/workflows/imp").resolve()


def _phase(wf, name):
    return next(p for p in wf.phases if p.name == name)


def test_stamp_and_finalize_tail_the_implement_phase() -> None:
    wf = load_workflow(WF)
    impl = [s.name for s in _phase(wf, "implement").steps]
    assert impl[-2:] == ["stamp-trailers", "finalize"]
    assert not any(p.name == "review" for p in wf.phases)


def test_merge_phase_linear_queue_flow() -> None:
    wf = load_workflow(WF)
    steps = _phase(wf, "merge").steps
    names = [s.name for s in steps]
    assert names == [
        "wait-for-head", "integrate-rebase", "integrator", "integration-reviewer",
        "integration-verify", "integration-resolver", "merge-to-base",
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
    # exit 2 -> merge directly (no review)
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 2})
    assert isinstance(m.action, Goto) and m.action.target == "merge-to-base"
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
    assert isinstance(m.action, Goto) and m.action.target == "merge-to-base"
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
