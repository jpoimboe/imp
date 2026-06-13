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
    # Span [18..23] = integration-gate, integration-pass, integration-sweep,
    # integration-sweep-tests, stamp-sweep-trailers, integrate-merge. Gates:
    # integration-pass (goto task-lead backward), integration-sweep-tests (resume),
    # stamp-sweep-trailers (pause), integrate-merge (pause). integration-gate
    # (forward goto) and integration-sweep (continue-only) are NOT gates.
    assert gate_steps_in_span(wf, 18, 23) == (
        "integration-pass", "integration-sweep-tests",
        "stamp-sweep-trailers", "integrate-merge",
    )


def test_gate_steps_in_span_detects_pause_only_gates() -> None:
    from imp.engine.recovery import gate_steps_in_span

    # autosquash(11)/stamp-trailers(12) gate via `pause: true` (no goto).
    assert gate_steps_in_span(_wf(), 11, 12) == ("autosquash", "stamp-trailers")


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
        plan_rewind(_wf(), _state("review"), "no-such-step", force=False)
    msg = str(exc.value)
    assert "no-such-step" in msg
    assert "review" in msg                  # a valid step is listed
    assert "merge" in msg                   # a valid phase is listed


def test_plan_rewind_backward_to_step() -> None:
    from imp.engine.recovery import plan_rewind

    plan = plan_rewind(_wf(), _state("review"), "review", force=False)
    assert plan.to_step == "review"
    assert plan.target_idx == 13
    assert plan.bypass_gates == ()
    assert plan.set_ready_to_merge is False
    assert plan.from_step == "review"


def test_plan_rewind_forward_to_merge_without_force_refused() -> None:
    from imp.engine.recovery import GateBypassRequired, plan_rewind

    with pytest.raises(GateBypassRequired) as exc:
        plan_rewind(_wf(), _state("integrate-merge"), "merge", force=False)
    assert exc.value.gate_steps == (
        "integrate-merge", "integrate-tests", "integration-review", "final-review",
    )
    assert "integrate-merge" in str(exc.value)


def test_plan_rewind_forward_to_merge_with_force() -> None:
    from imp.engine.recovery import plan_rewind

    plan = plan_rewind(_wf(), _state("integrate-merge"), "merge", force=True)
    assert plan.to_step == "base-fresh-check"        # merge phase entry
    assert plan.target_idx == 30
    assert plan.set_ready_to_merge is True
    assert plan.bypass_gates == (
        "integrate-merge", "integrate-tests", "integration-review", "final-review",
    )
    # span [cursor 23, target_idx-1 29]: merge-phase steps (base-fresh-check 30,
    # merge-to-base 31) are NOT bypassed — they run normally.
    assert "base-fresh-check" not in plan.bypass_gates
    assert "merge-to-base" not in plan.bypass_gates


def test_plan_rewind_merge_blocked_reentry_no_force_no_gates() -> None:
    from imp.engine.recovery import plan_rewind

    # merge_blocked: cursor None (terminal), ready_to_merge already true.
    st = _state(None, vars={"ready_to_merge": True})
    plan = plan_rewind(_wf(), st, "merge", force=False)
    assert plan.to_step == "base-fresh-check"        # B3: re-enters the merge phase
    assert plan.from_step is None                    # nullable from a None cursor
    assert plan.bypass_gates == ()                   # backward jump (target < terminal cursor)
    assert plan.set_ready_to_merge is False


def test_apply_rewind_resets_downstream_loop_counts_and_emits() -> None:
    from imp.engine.recovery import apply_rewind, plan_rewind

    wf = _wf()
    # review (13) maxed; task-lead (8) upstream; final-review (29) downstream.
    st = _state("review",
                loop_counts={"task-lead": 2, "review": 6, "final-review": 1})
    log = InMemoryEventLog()
    plan = plan_rewind(wf, st, "review", force=False)
    apply_rewind(plan, wf, st, log)

    assert st.step == "review"
    assert st.step_attempt == 0
    assert st.resume_pending is None
    assert st.current_step is None
    assert st.loop_counts == {"task-lead": 2}        # downstream reset, upstream kept
    types = [e["type"] for e in log.read()]
    assert "step_rewound" in types
    rewound = next(e for e in log.read() if e["type"] == "step_rewound")
    assert rewound["payload"]["from_step"] == "review"
    assert rewound["payload"]["to_target"] == "review"


def test_apply_rewind_force_sets_var_and_emits_gates_bypassed() -> None:
    from imp.engine.recovery import apply_rewind, plan_rewind

    wf = _wf()
    st = _state("integrate-merge")
    log = InMemoryEventLog()
    plan = plan_rewind(wf, st, "merge", force=True)
    apply_rewind(plan, wf, st, log)

    assert st.step == "base-fresh-check"             # merge phase entry
    assert st.vars["ready_to_merge"] is True
    bypassed = next(e for e in log.read() if e["type"] == "gates_bypassed")
    assert bypassed["payload"]["gates"] == [
        "integrate-merge", "integrate-tests", "integration-review", "final-review",
    ]


def test_plan_rewind_forward_no_gate_allowed_without_force() -> None:
    from imp.engine.recovery import plan_rewind

    # summarizer (27) -> final-review (29): span [27,28] = summarizer +
    # final-review-gate, both non-gates -> a forward jump bypassing no gate.
    plan = plan_rewind(_wf(), _state("summarizer"), "final-review", force=False)
    assert plan.to_step == "final-review"
    assert plan.bypass_gates == ()
    assert plan.set_ready_to_merge is False
