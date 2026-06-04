from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow

_ROLES = frozenset({
    "default", "spec", "spec_review", "plan", "plan_review", "task_lead",
    "code", "code_review", "fast", "balanced", "powerful",
})


def _wf():
    return load_workflow(default_workflow_dir(), configured_roles=_ROLES)


def test_gate_steps_in_span_review_phase() -> None:
    from imp.engine.recovery import gate_steps_in_span

    wf = _wf()
    # Span [17..20] = code-reviewer, summarizer, final-review-gate, final-review.
    # Gates: code-reviewer (goto implement/plan-writer backward) + final-review
    # (goto implement backward / pause). summarizer (no rules) and
    # final-review-gate (goto merge forward only) are NOT gates.
    assert gate_steps_in_span(wf, 17, 20) == ("code-reviewer", "final-review")


def test_gate_steps_in_span_detects_pause_only_gates() -> None:
    from imp.engine.recovery import gate_steps_in_span

    # autosquash(15)/stamp-trailers(16) gate via `pause: true` (no goto) — the
    # spec's headline reason _is_gate must check Pause, not just backward goto.
    assert gate_steps_in_span(_wf(), 15, 16) == ("autosquash", "stamp-trailers")


def test_gate_steps_in_span_empty_when_lo_gt_hi() -> None:
    from imp.engine.recovery import gate_steps_in_span

    assert gate_steps_in_span(_wf(), 5, 4) == ()
