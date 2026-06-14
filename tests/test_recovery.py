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
    # Span [17..20] = integrate-merge, integrator, integrate-tests, finalize.
    # Gates: integrate-merge (pause), integrate-tests (resume: integrator, backward),
    # finalize (pause + goto implement backward).
    # integrator (no rules / continue-only) is NOT a gate.
    assert gate_steps_in_span(wf, 17, 20) == (
        "integrate-merge", "integrate-tests", "finalize",
    )


def test_gate_steps_in_span_detects_pause_only_gates() -> None:
    from imp.engine.recovery import gate_steps_in_span

    # autosquash(10) and stamp-trailers(15) gate via `pause: true` (no goto).
    assert gate_steps_in_span(_wf(), 10, 10) == ("autosquash",)
    assert gate_steps_in_span(_wf(), 15, 15) == ("stamp-trailers",)


def test_gate_steps_in_span_empty_when_lo_gt_hi() -> None:
    from imp.engine.recovery import gate_steps_in_span

    assert gate_steps_in_span(_wf(), 5, 4) == ()


def _state(step, *, loop_counts=None, vars=None):
    return State(
        id="s", step=step, step_attempt=3, status="paused",
        vars=vars if vars is not None else {},
        loop_counts=loop_counts or {},
        resume_pending="something", current_step=None,
    )


def test_plan_rewind_unknown_target_lists_names() -> None:
    from imp.engine.recovery import RewindError, plan_rewind

    with pytest.raises(RewindError) as exc:
        plan_rewind(_wf(), _state("review"), "no-such-step")
    msg = str(exc.value)
    assert "no-such-step" in msg
    assert "review" in msg                  # a valid step is listed
    assert "merge" in msg                   # a valid phase is listed


def test_plan_rewind_backward_to_step() -> None:
    from imp.engine.recovery import plan_rewind

    plan = plan_rewind(_wf(), _state("review"), "review")
    assert plan.to_step == "review"
    assert plan.target_idx == 12
    assert plan.from_step == "review"


def test_plan_rewind_forward_to_merge_is_rejected() -> None:
    from imp.engine.recovery import GateBypassRequired, plan_rewind

    with pytest.raises(GateBypassRequired) as exc:
        plan_rewind(_wf(), _state("integrate-merge"), "merge")
    assert exc.value.gate_steps == (
        "integrate-merge", "integrate-tests", "finalize",
    )
    msg = str(exc.value)
    assert "integrate-merge" in msg
    assert "--force" not in msg
    assert "not allowed" in msg


def test_plan_rewind_merge_blocked_reentry_no_gates() -> None:
    from imp.engine.recovery import plan_rewind

    # cursor None (terminal): the merge-blocked state; vars are opaque to recovery.
    st = _state(None)
    plan = plan_rewind(_wf(), st, "merge")
    assert plan.to_step == "base-fresh-check"        # B3: re-enters the merge phase
    assert plan.from_step is None                    # nullable from a None cursor


def test_apply_rewind_resets_downstream_loop_counts_and_emits() -> None:
    from imp.engine.recovery import apply_rewind, plan_rewind

    wf = _wf()
    # review (12) maxed; task-lead (7) upstream; finalize (20) downstream.
    st = _state("review",
                loop_counts={"task-lead": 2, "review": 6, "finalize": 1})
    log = InMemoryEventLog()
    plan = plan_rewind(wf, st, "review")
    apply_rewind(plan, wf, st, log)

    assert st.step == "review"
    assert st.step_attempt == 0
    assert st.resume_pending is None
    assert st.current_step is None
    assert st.loop_counts == {"task-lead": 2}        # downstream reset, upstream kept
    assert st.vars == {}                             # untouched by any arming
    types = [e["type"] for e in log.read()]
    assert "step_rewound" in types
    assert "gates_bypassed" not in types
    rewound = next(e for e in log.read() if e["type"] == "step_rewound")
    assert rewound["payload"]["from_step"] == "review"
    assert rewound["payload"]["to_target"] == "review"


def test_plan_rewind_forward_no_gate_allowed() -> None:
    from imp.engine.recovery import plan_rewind

    # integrator (18) -> integrate-tests (19): span [18,18] = integrator,
    # a non-gate (no rules / continue-only) -> a forward jump bypassing no gate.
    plan = plan_rewind(_wf(), _state("integrator"), "integrate-tests")
    assert plan.to_step == "integrate-tests"
