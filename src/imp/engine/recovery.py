"""Change recovery primitives for `imp resume --from`
(spec docs/.../2026-06-04-resume-from-and-abandon.md).

The CLI owns the I/O (lock, load, save, run); this module is the pure decision
+ mutation layer so it is unit-testable without git, providers, or a live run.
"""

from __future__ import annotations

from imp.engine.state import EventLog, State  # noqa: F401  (used by later tasks)
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
