"""The implement phase no longer carries a per-plan cleanup step: fold + stamp is
deferred to the single end-of-change cleanup-merge. task-lead advances straight to
verify, then check-next-plan; a tolerated task-lead routes directly to
check-next-plan."""
from __future__ import annotations

from pathlib import Path

from imp.engine.loader import load_workflow
from imp.engine.types import Continue, Goto

WF_DIR = Path("src/imp/assets/workflows/imp")


def _wf():
    return load_workflow(WF_DIR)


def _steps(wf):
    return {s.name: s for s in wf.flat_steps()}


def test_autosquash_step_is_gone() -> None:
    names = [s.name for s in _wf().flat_steps()]
    assert "autosquash" not in names
    assert "review-base-init" not in names
    assert "commit-check" not in names


def test_implement_cleanup_steps_removed() -> None:
    names = [s.name for s in _wf().flat_steps()]
    assert "cleanup-implement" not in names
    assert "autosquash-resolve-implement" not in names


def test_implement_phase_is_task_lead_verify_check_next_plan() -> None:
    from imp.engine.gates import evaluate_rules
    wf = _wf()
    impl = next(p for p in wf.phases if p.name == "implement")
    assert [s.name for s in impl.steps] == ["task-lead", "verify", "check-next-plan"]
    # verify green continues positionally into check-next-plan (no cleanup between).
    verify = _steps(wf)["verify"]
    assert isinstance(evaluate_rules(verify.rules, vars={}, output={"exit": 0}).action, Continue)


def test_task_lead_tolerated_routes_to_check_next_plan() -> None:
    from imp.engine.gates import evaluate_rules
    step = _steps(_wf())["task-lead"]
    m = evaluate_rules(step.rules, vars={},
                       output={"status": "Success", "verify_tolerated": True, "summary": "x"})
    assert isinstance(m.action, Goto) and m.action.target == "check-next-plan"
