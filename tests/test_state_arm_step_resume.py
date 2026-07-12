from __future__ import annotations

from imp.engine.state import State


def _state(step: str = "s") -> State:
    return State(id="c", step=step, step_attempt=0, status="new")


def test_arm_continue_sets_pending_and_mode() -> None:
    s = _state("plan")
    s.arm_step_resume(mode="continue")
    assert s.resume_pending == "plan"
    assert s.resume_mode == "continue"


def test_arm_solicit_sets_pending_and_mode() -> None:
    s = _state("plan")
    s.arm_step_resume(mode="solicit")
    assert s.resume_pending == "plan"
    assert s.resume_mode == "solicit"


def test_arm_targets_the_current_step() -> None:
    s = _state("review")
    s.arm_step_resume(mode="continue")
    assert s.resume_pending == s.step


def test_clear_step_resume_clears_both() -> None:
    s = _state("plan")
    s.arm_step_resume(mode="continue")
    s.clear_step_resume()
    assert s.resume_pending is None
    assert s.resume_mode is None
