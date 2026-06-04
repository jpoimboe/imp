"""Change recovery primitives for `imp resume --from`
(spec docs/.../2026-06-04-resume-from-and-abandon.md).

The CLI owns the I/O (lock, load, save, run); this module is the pure decision
+ mutation layer so it is unit-testable without git, providers, or a live run.
"""

from __future__ import annotations

from dataclasses import dataclass

from imp.engine.state import EventLog, State
from imp.engine.types import Goto, Pause, Step, Workflow


class RewindError(Exception):
    """An unresolvable --from target. Its message lists the valid phase and
    step names (so the CLI prints a friendly error, never a KeyError)."""


class GateBypassRequired(Exception):
    """A forward --from jump would skip one or more gate steps and --force was
    not given. gate_steps are the skipped gates (for the message + tests)."""

    def __init__(self, gate_steps: tuple[str, ...], message: str) -> None:
        self.gate_steps = gate_steps
        super().__init__(message)


def _is_gate(workflow: Workflow, step: Step, idx: int) -> bool:
    """True if step can block or send work back: any rule/option action that
    pauses, or goes to an equal-or-lower flat index (backward/self). A
    forward-only goto (e.g. final-review-gate -> merge) is not a gate.

    Scope is deliberately rules/options only — NOT on_max_retries. The spec
    defines a gate by its authored rules; the default workflow's pause-only
    steps (autosquash/stamp-trailers) use explicit `else: pause` rules. Do not
    fold on_max_retries in here without revisiting the spec definition."""

    def blocks(action: object) -> bool:
        if isinstance(action, Pause):
            return True
        if isinstance(action, Goto):
            return workflow.resolve_goto(action.target) <= idx
        return False

    return (any(blocks(r.action) for r in step.rules)
            or any(blocks(o.action) for o in step.options))


def gate_steps_in_span(workflow: Workflow, lo: int, hi: int) -> tuple[str, ...]:
    """Names of gate steps on flat indices [lo, hi] inclusive (() if lo > hi)."""
    flat = workflow.flat_steps()
    return tuple(s.name for i, s in enumerate(flat)
                 if lo <= i <= hi and _is_gate(workflow, s, i))


@dataclass(frozen=True)
class RewindPlan:
    target: str                     # the raw --from target string (for events)
    from_step: str | None           # cursor before the rewind (nullable)
    to_step: str | None             # None => terminal (re-enter _complete)
    target_idx: int                 # resolve_goto(target)
    bypass_gates: tuple[str, ...]   # () unless a forced forward jump skips gates
    set_ready_to_merge: bool


def _unknown_target_message(workflow: Workflow, target: str) -> str:
    phases = ", ".join(p.name for p in workflow.phases)
    steps = ", ".join(s.name for s in workflow.flat_steps())
    return (f"imp: unknown --from target {target!r}.\n"
            f"  valid phases: {phases}\n"
            f"  valid steps:  {steps}")


def plan_rewind(workflow: Workflow, state: State, target: str,
                *, force: bool) -> RewindPlan:
    """Resolve target, decide direction, compute bypassed gates. Raises
    RewindError (unknown target) or GateBypassRequired (forward jump skipping a
    gate without --force). Performs NO mutation and emits NO events."""
    flat = workflow.flat_steps()
    index_of = {s.name: i for i, s in enumerate(flat)}
    try:
        target_idx = workflow.resolve_goto(target)
    except KeyError:
        raise RewindError(_unknown_target_message(workflow, target)) from None

    # A None / terminal / stale cursor counts as "at the end" (len(flat)).
    cursor_idx = index_of.get(state.step, len(flat)) if state.step else len(flat)

    bypass_gates: tuple[str, ...] = ()
    if target_idx > cursor_idx:                      # forward jump
        bypass_gates = gate_steps_in_span(workflow, cursor_idx, target_idx - 1)
        if bypass_gates and not force:
            raise GateBypassRequired(
                bypass_gates,
                f"imp: --from {target!r} would skip gate step(s): "
                f"{', '.join(bypass_gates)}.\n"
                f"  re-run with --force to land the Change as-is.")

    to_step = None if target_idx >= len(flat) else flat[target_idx].name
    return RewindPlan(
        target=target, from_step=state.step, to_step=to_step,
        target_idx=target_idx, bypass_gates=bypass_gates,
        set_ready_to_merge=bool(bypass_gates))


def apply_rewind(plan: RewindPlan, workflow: Workflow, state: State,
                 log: EventLog) -> None:
    """Mutate state to re-enter at the planned target, then append the marker
    events. Call ONLY after plan_rewind returned without raising."""
    flat = workflow.flat_steps()
    # Reset loop counts for the target step and everything downstream (higher
    # flat index). Upstream budgets are intentionally preserved (re-entry only
    # goes forward from the target, so already-exhausted upstream loops keep
    # their spent counts).
    for i, s in enumerate(flat):
        if i >= plan.target_idx:
            state.loop_counts.pop(s.name, None)

    state.step = plan.to_step
    state.current_step = None
    state.resume_pending = None
    state.step_attempt = 0

    if plan.set_ready_to_merge:
        state.vars["ready_to_merge"] = True
        log.append(agent_id=None, type="gates_bypassed",
                   payload={"gates": list(plan.bypass_gates),
                            "to_target": plan.target})
    log.append(agent_id=None, type="step_rewound",
               payload={"from_step": plan.from_step, "to_target": plan.target,
                        "reason": "resume --from"})
