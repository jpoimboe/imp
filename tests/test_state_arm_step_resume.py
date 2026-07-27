from __future__ import annotations

from imp.engine.state import State


def _state(step: str = "s") -> State:
    return State(id="c", step=step, step_attempt=0, status="new")


def test_arm_continue_sets_pending_and_flag() -> None:
    s = _state("plan")
    s.arm_step_resume(should_continue=True)
    assert s.resume_pending == "plan"
    assert s.resume_should_continue is True


def test_arm_no_continue_sets_pending_and_flag() -> None:
    s = _state("plan")
    s.arm_step_resume(should_continue=False)
    assert s.resume_pending == "plan"
    assert s.resume_should_continue is False


def test_arm_targets_the_current_step() -> None:
    s = _state("review")
    s.arm_step_resume(should_continue=True)
    assert s.resume_pending == s.step


def test_clear_step_resume_clears_both() -> None:
    s = _state("plan")
    s.arm_step_resume(should_continue=True)
    s.clear_step_resume()
    assert s.resume_pending is None
    assert s.resume_should_continue is False


def test_arm_defaults_the_cause_to_human() -> None:
    s = _state("plan")
    s.arm_step_resume(should_continue=True)
    assert s.resume_cause == "human"


def test_arm_carries_the_provider_retry_cause() -> None:
    s = _state("plan")
    s.arm_step_resume(should_continue=True, cause="provider_retry")
    assert s.resume_cause == "provider_retry"


def test_a_later_arm_resets_the_cause_to_human() -> None:
    """A cause armed for one backoff retry must not survive into a human pause."""
    s = _state("plan")
    s.arm_step_resume(should_continue=True, cause="provider_retry")
    s.arm_step_resume(should_continue=False)
    assert s.resume_cause == "human"


def test_clear_step_resume_clears_the_cause() -> None:
    s = _state("plan")
    s.arm_step_resume(should_continue=True, cause="provider_retry")
    s.clear_step_resume()
    assert s.resume_cause == "human"
