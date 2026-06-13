"""Single read-once accessor for the IMP_MAX_PARALLEL_TASKS knob (design
Decision 5 — the width cap).

Leaf module: imports nothing from imp, so dispatch_task (agents.subagents),
ToolContext (agents.tools.context), and the later batch helper / bridges can all
consume the SAME resolved value with no import cycle. The knob is read, clamped,
and warned exactly ONCE per process through `resolve_parallelism`; tests override
through the `reset_parallelism_cache` seam, never by mutating env mid-process.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

DEFAULT_MAX_PARALLEL_TASKS = 8
_MIN_PARALLEL_TASKS = 1
_MAX_PARALLEL_TASKS = 64
_ENV_VAR = "IMP_MAX_PARALLEL_TASKS"


@dataclass(frozen=True)
class ParallelismSetting:
    """The resolved width plus an optional one-line clamp warning (None when the
    knob was absent or already in range)."""

    value: int
    warning: str | None


def _resolve(raw: str | None) -> ParallelismSetting:
    """Pure clamp of a raw env string. Failure direction is LESS parallelism,
    never a crash: unparseable or out-of-range inputs clamp into [1, 64] with a
    warning rather than raising (a typo'd escape hatch must not crash dispatch)."""
    if raw is None:
        return ParallelismSetting(DEFAULT_MAX_PARALLEL_TASKS, None)
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return ParallelismSetting(
            _MIN_PARALLEL_TASKS,
            f"{_ENV_VAR}={raw!r} is not an integer; clamped to "
            f"{_MIN_PARALLEL_TASKS}",
        )
    if n < _MIN_PARALLEL_TASKS:
        return ParallelismSetting(
            _MIN_PARALLEL_TASKS,
            f"{_ENV_VAR}={n} below minimum; clamped to {_MIN_PARALLEL_TASKS}",
        )
    if n > _MAX_PARALLEL_TASKS:
        return ParallelismSetting(
            _MAX_PARALLEL_TASKS,
            f"{_ENV_VAR}={n} above maximum; clamped to {_MAX_PARALLEL_TASKS}",
        )
    return ParallelismSetting(n, None)


_resolved: ParallelismSetting | None = None
_warning_emitted = False


def resolve_parallelism() -> ParallelismSetting:
    """Resolve the knob once per process and cache it (the single read site)."""
    global _resolved
    if _resolved is None:
        _resolved = _resolve(os.environ.get(_ENV_VAR))
    return _resolved


def max_parallel_tasks() -> int:
    """The resolved, clamped limiter width."""
    return resolve_parallelism().value


def reset_parallelism_cache() -> None:
    """Test seam: drop the cached resolution AND the emit-once flag so the next
    accessor call re-reads env and the warning can fire again. Never called in
    production."""
    global _resolved, _warning_emitted
    _resolved = None
    _warning_emitted = False


def emit_parallelism_warning_once(event_log: Any, agent_id: str | None) -> None:
    """Emit the clamp `runner_warning` at most once per process (design Decision
    5 — warned ONCE at startup). The first dispatch to reach here drives the
    emit; later dispatches anywhere in the process are no-ops. Best-effort: a
    None event_log (e.g. a test/legacy context) drops the warning but still
    latches the once-flag."""
    global _warning_emitted
    if _warning_emitted:
        return
    _warning_emitted = True
    setting = resolve_parallelism()
    if setting.warning is None:
        return
    if event_log is not None:
        event_log.append(
            agent_id=agent_id, type="runner_warning",
            payload={"message": setting.warning},
        )
