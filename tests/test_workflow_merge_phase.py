from pathlib import Path

from imp.engine.gates import evaluate_rules
from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto, Pause

WF = Path("src/imp/assets/workflows/imp").resolve()


def _phase(wf, name):
    return next(p for p in wf.phases if p.name == name)


def test_review_phase_is_just_finalize() -> None:
    wf = load_workflow(WF)
    assert [s.name for s in _phase(wf, "review").steps] == ["finalize"]


def test_merge_phase_linear_queue_flow() -> None:
    wf = load_workflow(WF)
    names = [s.name for s in _phase(wf, "merge").steps]
    assert names == [
        "wait-for-head", "integrate-rebase", "integrator", "integration-review",
        "integration-verify", "bypass-dequeue", "integration-resolve", "merge-to-base",
    ]
    kinds = {s.name: s.kind for s in _phase(wf, "merge").steps}
    assert kinds["wait-for-head"] == "wait_for_head"
    assert kinds["bypass-dequeue"] == "dequeue_wake"
    assert kinds["merge-to-base"] == "merge"


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
    assert isinstance(m.action, Goto) and m.action.target == "integration-review"
    # exit 6 -> integrator (continue)
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 6})
    assert isinstance(m.action, Continue)
    # else (e.g., exit 5) -> pause
    m = evaluate_rules(rules, vars=_BASE, output={"exit": 5})
    assert isinstance(m.action, Pause)


def test_integration_review_routing() -> None:
    wf = load_workflow(WF)
    rules = next(s for s in wf.flat_steps() if s.name == "integration-review").rules
    _BASE = {}
    m = evaluate_rules(rules, vars=_BASE, output={"verdict": "breakage", "status": "Success", "summary": ""})
    assert isinstance(m.action, Goto) and m.action.target == "bypass-dequeue"
    m = evaluate_rules(rules, vars=_BASE, output={"verdict": "ok", "status": "Success", "summary": ""})
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
    assert isinstance(m.action, Goto) and m.action.target == "bypass-dequeue"


def test_integration_resolve_routing() -> None:
    wf = load_workflow(WF)
    rules = next(s for s in wf.flat_steps() if s.name == "integration-resolve").rules
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
