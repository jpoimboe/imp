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


_POLICY: dict[
    Category,
    tuple[type[Pause] | type[RetryNow] | type[RetryWithBackoff], int | None],
] = {
    "auth":                 (RetryWithBackoff, 7),
    "config":               (Pause, None),
    "quota":                (Pause, None),
    "content_filter":       (Pause, None),
    "context_window":       (Pause, None),
    "rate_limit":           (RetryWithBackoff, None),
    "network":              (RetryWithBackoff, None),
    "api":                  (Pause, None),
    "loop":                 (Pause, None),
    "agent_output_missing": (RetryNow, None),
    "agent_incomplete":     (Pause, None),
    "terminated":           (Pause, None),
    "unknown":              (Pause, None),
}

def decide(
    category: Category,
    *,
    prior_backoff_attempts: int,
    hint_delay_seconds: float | None = None,
) -> ErrorDecision:
    """Map a category + prior-attempts count to a decision.

    Most RetryWithBackoff categories retry forever: delay = 1·2^n, unbounded.
    `auth` is capped at 7 attempts — beyond that it Pauses for human attention.
    `hint_delay_seconds` (a server Retry-After) is honored as max(hint, our delay)
    but capped at 60s so a misbehaving upstream header can't strand us; that 60s
    cap is on the *hint only*, not on our own doubling.
    """
    cls, cap = _POLICY.get(category, (Pause, None))
    if cls is Pause:
        return Pause()
    if cls is RetryNow:
        return RetryNow()
    if cap is not None and prior_backoff_attempts >= cap:
        return Pause()
    delay = 1.0 * (2 ** prior_backoff_attempts)
    if hint_delay_seconds is not None:
        delay = min(60.0, max(delay, hint_delay_seconds))
    return RetryWithBackoff(delay)
