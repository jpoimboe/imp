import pytest

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog, State

_TIERS = frozenset({"low", "medium", "high"})


def _wf():
    return load_workflow(default_workflow_dir(), configured_tiers=_TIERS)


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


def _state(step, *, loop_counts=None, vars=None):
    return State(
        id="s", step=step, step_attempt=3, status="paused",
        vars=vars if vars is not None else {"ready_to_merge": False},
        loop_counts=loop_counts or {},
        resume_pending="something", current_step=None,
    )


def test_plan_rewind_unknown_target_lists_names() -> None:
    from imp.engine.recovery import RewindError, plan_rewind

    with pytest.raises(RewindError) as exc:
        plan_rewind(_wf(), _state("code-reviewer"), "no-such-step", force=False)
    msg = str(exc.value)
    assert "no-such-step" in msg
    assert "code-reviewer" in msg          # a valid step is listed
    assert "merge" in msg                  # a valid phase is listed


def test_plan_rewind_backward_to_step() -> None:
    from imp.engine.recovery import plan_rewind

    plan = plan_rewind(_wf(), _state("code-reviewer"), "code-reviewer", force=False)
    assert plan.to_step == "code-reviewer"
    assert plan.target_idx == 17
    assert plan.bypass_gates == ()
    assert plan.set_ready_to_merge is False
    assert plan.from_step == "code-reviewer"


def test_plan_rewind_forward_to_merge_without_force_refused() -> None:
    from imp.engine.recovery import GateBypassRequired, plan_rewind

    with pytest.raises(GateBypassRequired) as exc:
        plan_rewind(_wf(), _state("code-reviewer"), "merge", force=False)
    # The overridden cursor gate + the downstream gate are both named.
    assert exc.value.gate_steps == ("code-reviewer", "final-review")
    assert "code-reviewer" in str(exc.value)


def test_plan_rewind_forward_to_merge_with_force() -> None:
    from imp.engine.recovery import plan_rewind

    plan = plan_rewind(_wf(), _state("code-reviewer"), "merge", force=True)
    assert plan.to_step is None                      # terminal
    assert plan.target_idx == 21
    assert plan.set_ready_to_merge is True
    assert plan.bypass_gates == ("code-reviewer", "final-review")


def test_plan_rewind_merge_blocked_reentry_no_force_no_gates() -> None:
    from imp.engine.recovery import plan_rewind

    # merge_blocked: cursor None (terminal), ready_to_merge already true.
    st = _state(None, vars={"ready_to_merge": True})
    plan = plan_rewind(_wf(), st, "merge", force=False)
    assert plan.to_step is None
    assert plan.from_step is None                    # nullable from a None cursor
    assert plan.bypass_gates == ()                   # forward jump? no — equal index
    assert plan.set_ready_to_merge is False


def test_apply_rewind_resets_downstream_loop_counts_and_emits() -> None:
    from imp.engine.recovery import apply_rewind, plan_rewind

    wf = _wf()
    # code-reviewer (17) maxed; task-lead (10) upstream; final-review (20) downstream.
    st = _state("code-reviewer",
                loop_counts={"task-lead": 2, "code-reviewer": 6, "final-review": 1})
    log = InMemoryEventLog()
    plan = plan_rewind(wf, st, "code-reviewer", force=False)
    apply_rewind(plan, wf, st, log)

    assert st.step == "code-reviewer"
    assert st.step_attempt == 0
    assert st.resume_pending is None
    assert st.current_step is None
    assert st.loop_counts == {"task-lead": 2}        # downstream reset, upstream kept
    types = [e["type"] for e in log.read()]
    assert "step_rewound" in types
    rewound = next(e for e in log.read() if e["type"] == "step_rewound")
    assert rewound["payload"]["from_step"] == "code-reviewer"
    assert rewound["payload"]["to_target"] == "code-reviewer"


def test_apply_rewind_force_sets_var_and_emits_gates_bypassed() -> None:
    from imp.engine.recovery import apply_rewind, plan_rewind

    wf = _wf()
    st = _state("code-reviewer")
    log = InMemoryEventLog()
    plan = plan_rewind(wf, st, "merge", force=True)
    apply_rewind(plan, wf, st, log)

    assert st.step is None
    assert st.vars["ready_to_merge"] is True
    bypassed = next(e for e in log.read() if e["type"] == "gates_bypassed")
    assert bypassed["payload"]["gates"] == ["code-reviewer", "final-review"]


def test_plan_rewind_forward_no_gate_allowed_without_force() -> None:
    from imp.engine.recovery import plan_rewind

    # summarizer (18) -> final-review (20): span [18,19] = summarizer +
    # final-review-gate, both non-gates -> a forward jump bypassing no gate,
    # so it is allowed without --force and sets no ready_to_merge.
    plan = plan_rewind(_wf(), _state("summarizer"), "final-review", force=False)
    assert plan.to_step == "final-review"
    assert plan.bypass_gates == ()
    assert plan.set_ready_to_merge is False
