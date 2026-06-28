"""Per-category executor policy for provider errors.

Pure functions + data. No I/O. Consumed by ``imp.engine.executor`` to
decide between Pause / RetryNow / RetryWithBackoff for each
provider-emitted ``error`` event.

The retry-with-backoff categories use a separate attempt counter from
``step.max_retries`` — a flaky network shouldn't burn through the
agent's "you have 3 chances to produce valid output" budget. Counter
lives in local executor variables; see ``_route_agent`` /
``_run_agent_invocation`` in ``executor.py``.
"""

from __future__ import annotations

from dataclasses import dataclass

from imp.providers.errors import Category


@dataclass(frozen=True)
class Pause:
    pass


@dataclass(frozen=True)
class RetryNow:
    pass


@dataclass(frozen=True)
class RetryWithBackoff:
    delay_seconds: float


ErrorDecision = Pause | RetryNow | RetryWithBackoff


@dataclass(frozen=True)
class _Policy:
    decision: type[Pause] | type[RetryNow] | type[RetryWithBackoff]
    budget_seconds: float | None


# One give-up concept: a per-category wall-clock budget (seconds), identical in
# interactive and headless runs. RetryWithBackoff categories carry a budget; the
# executor/subagent loop pauses once cumulative backoff would exceed it. Pause and
# RetryNow categories have no budget (None).
_POLICY: dict[str, _Policy] = {
    # Patient: a 401 is typically a transient refresh-path failure that heals on
    # its own — the CLI auto-refreshes OAuth per spawn, so the 401 surfaces only
    # after its own refresh attempts transiently fail. Same budget as network /
    # rate_limit, not a tight one that gives up mid-heal.
    "auth":                 _Policy(RetryWithBackoff, 1800.0),
    "config":               _Policy(Pause, None),
    "quota":                _Policy(Pause, None),
    "content_filter":       _Policy(Pause, None),
    "context_window":       _Policy(Pause, None),
    "rate_limit":           _Policy(RetryWithBackoff, 1800.0),
    "network":              _Policy(RetryWithBackoff, 1800.0),
    # A B.2 watchdog stall is a transient wedge: retry with budgeted backoff like
    # network / rate_limit rather than pausing as a hard error. 1800 s matches the
    # other transient budgets.
    "stalled":              _Policy(RetryWithBackoff, 1800.0),
    "api":                  _Policy(Pause, None),
    "loop":                 _Policy(Pause, None),
    "agent_output_missing": _Policy(RetryNow, None),
    "agent_incomplete":     _Policy(Pause, None),
    "terminated":           _Policy(Pause, None),
    "unknown":              _Policy(Pause, None),
}


def budget_for(category: str) -> float | None:
    """The per-category retry wall-clock budget in seconds, or None for
    categories that never retry-with-backoff. Read by the executor and subagent
    backoff loops to decide when to pause. Accepts the plumbed-out
    ``ErrorInfo.category`` (a plain str); an unrecognized category yields None."""
    policy = _POLICY.get(category)
    return policy.budget_seconds if policy is not None else None


def decide(
    category: Category,
    *,
    prior_backoff_attempts: int,
    hint_delay_seconds: float | None = None,
) -> ErrorDecision:
    """Map a category + prior-attempts count to a decision.

    One delay formula for every backoff category, always 60s-capped:
    ``min(60, max(2 ** prior_backoff_attempts, hint_delay_seconds or 0))`` →
    schedule 1, 2, 4, 8, 16, 32, 60, 60, …. A server Retry-After hint is just
    another term inside the same min/max — there is no separate hint-only branch
    and no attempt cap (give-up is the per-category budget, owned by the caller).
    """
    policy = _POLICY.get(category)
    if policy is None or policy.decision is Pause:
        return Pause()
    if policy.decision is RetryNow:
        return RetryNow()
    delay = min(60.0, max(2.0 ** prior_backoff_attempts, hint_delay_seconds or 0.0))
    return RetryWithBackoff(delay)
