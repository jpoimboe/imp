from __future__ import annotations

from imp.engine.state import State


def _state(step: str = "s") -> State:
    return State(id="c", step=step, step_attempt=0, status="running")


def test_arm_interrupt_true_sets_resume_pending_and_interrupt_pending() -> None:
    s = _state("plan")
    s.arm_step_resume(interrupt=True)
    assert s.resume_pending == "plan"
    assert s.interrupt_pending is True


def test_arm_interrupt_false_overwrites_stale_true() -> None:
    s = _state("plan")
    s.interrupt_pending = True  # stale from a prior interrupt+nudge
    s.arm_step_resume(interrupt=False)
    assert s.resume_pending == "plan"
    assert s.interrupt_pending is False  # unconditional assignment cleared it


def test_arm_targets_the_current_step() -> None:
    s = _state("review")
    s.arm_step_resume(interrupt=True)
    assert s.resume_pending == s.step
