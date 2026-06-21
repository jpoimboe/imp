import pytest

from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import InMemoryEventLog, State

_TIERS = frozenset({"low", "medium", "high"})


def _wf():
    return load_workflow(bundled_workflow_dir("imp"), configured_tiers=_TIERS)


def _idx(wf, name: str) -> int:
    return next(i for i, s in enumerate(wf.flat_steps()) if s.name == name)


def test_gate_steps_in_span_merge_phase() -> None:
    from imp.engine.recovery import gate_steps_in_span

    wf = _wf()
    # Merge phase gates (pause or backward/self goto), resolved by name so the
    # assertion is stable across the implement-phase step shuffle.
    lo, hi = _idx(wf, "integrate-rebase"), _idx(wf, "merge-to-base")
    assert gate_steps_in_span(wf, lo, hi) == (
        "integrate-rebase", "integration-verify", "cleanup-merge",
        "autosquash-resolve-merge", "integration-resolver", "assert-stamped",
        "merge-to-base",
    )


def test_gate_steps_in_span_detects_pause_only_gates() -> None:
    from imp.engine.recovery import gate_steps_in_span

    wf = _wf()
    # cleanup-implement gates via its `else: pause` rule; assert-stamped via its
    # `else: pause`. Each is the sole gate at its own flat index.
    i = _idx(wf, "cleanup-implement")
    assert gate_steps_in_span(wf, i, i) == ("cleanup-implement",)
    j = _idx(wf, "assert-stamped")
    assert gate_steps_in_span(wf, j, j) == ("assert-stamped",)


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
        plan_rewind(_wf(), _state("code-reviewer"), "no-such-step")
    msg = str(exc.value)
    assert "no-such-step" in msg
    assert "code-reviewer" in msg           # a valid step is listed
    assert "merge" in msg                   # a valid phase is listed


def test_plan_rewind_backward_to_step() -> None:
    from imp.engine.recovery import plan_rewind

    wf = _wf()
    plan = plan_rewind(wf, _state("code-reviewer"), "code-reviewer")
    assert plan.to_step == "code-reviewer"
    assert plan.target_idx == _idx(wf, "code-reviewer")   # name-resolved, shift-stable
    assert plan.from_step == "code-reviewer"


def test_plan_rewind_forward_to_merge_is_rejected() -> None:
    from imp.engine.recovery import GateBypassRequired, plan_rewind

    with pytest.raises(GateBypassRequired) as exc:
        plan_rewind(_wf(), _state("wait-for-head"), "merge-to-base")
    assert exc.value.gate_steps == (
        "integrate-rebase", "integration-verify", "cleanup-merge",
        "autosquash-resolve-merge", "integration-resolver", "assert-stamped",
    )
    msg = str(exc.value)
    assert "integrate-rebase" in msg
    assert "--force" not in msg
    assert "not allowed" in msg


def test_plan_rewind_merge_blocked_reentry_no_gates() -> None:
    from imp.engine.recovery import plan_rewind

    # cursor None (terminal): the merge-blocked state; vars are opaque to recovery.
    st = _state(None)
    plan = plan_rewind(_wf(), st, "merge")
    assert plan.to_step == "wait-for-head"           # re-enters the merge phase
    assert plan.from_step is None                    # nullable from a None cursor


def test_apply_rewind_resets_downstream_loop_counts_and_emits() -> None:
    from imp.engine.recovery import apply_rewind, plan_rewind

    wf = _wf()
    # code-reviewer maxed; task-lead upstream; finalize downstream.
    st = _state("code-reviewer",
                loop_counts={"task-lead": 2, "code-reviewer": 6, "finalize": 1})
    log = InMemoryEventLog()
    plan = plan_rewind(wf, st, "code-reviewer")
    apply_rewind(plan, wf, st, log)

    assert st.step == "code-reviewer"
    assert st.step_attempt == 0
    assert st.resume_pending is None
    assert st.current_step is None
    assert st.loop_counts == {"task-lead": 2}        # downstream reset, upstream kept
    assert st.vars == {}                             # untouched by any arming
    types = [e["type"] for e in log.read()]
    assert "step_rewound" in types
    assert "gates_bypassed" not in types
    rewound = next(e for e in log.read() if e["type"] == "step_rewound")
    assert rewound["payload"]["from_step"] == "code-reviewer"
    assert rewound["payload"]["to_target"] == "code-reviewer"


def test_plan_rewind_forward_no_gate_allowed() -> None:
    from imp.engine.recovery import plan_rewind

    # integrator -> integration-reviewer: span [integrator,integrator] = integrator,
    # a non-gate (no rules / continue-only) -> a forward jump bypassing no gate.
    plan = plan_rewind(_wf(), _state("integrator"), "integration-reviewer")
    assert plan.to_step == "integration-reviewer"


def test_apply_rewind_sets_step_label_from_target_step() -> None:
    from imp.engine.recovery import apply_rewind, plan_rewind

    wf = _wf()
    st = _state("code-reviewer")
    log = InMemoryEventLog()
    plan = plan_rewind(wf, st, "code-reviewer")
    apply_rewind(plan, wf, st, log)

    # step_label is the target step's resolved label (label or name), NOT None
    # and NOT a stale value from before the reroute.
    assert st.step == "code-reviewer"
    assert st.step_label == "reviewing code"


def test_apply_rewind_clears_step_label_when_to_step_is_none() -> None:
    """A terminal reroute (to_step=None, target_idx past the last flat index) takes
    apply_rewind's `else None` branch. Seed a stale step_label first so clearing it
    is falsifiable — the test fails if apply_rewind leaves the old label in place."""
    from imp.engine.recovery import RewindPlan, apply_rewind

    wf = _wf()
    flat = wf.flat_steps()
    st = _state("code-reviewer")
    st.step_label = "reviewing the code"          # stale label to be cleared
    log = InMemoryEventLog()
    plan = RewindPlan(target="end", from_step="code-reviewer",
                      to_step=None, target_idx=len(flat))
    apply_rewind(plan, wf, st, log)

    assert st.step is None
    assert st.step_label is None
