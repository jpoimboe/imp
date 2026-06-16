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


_POLICY: dict[Category, type[Pause] | type[RetryNow] | type[RetryWithBackoff]] = {
    "auth":                 Pause,
    "config":               Pause,
    "quota":                Pause,
    "content_filter":       Pause,
    "context_window":       Pause,
    "rate_limit":           RetryWithBackoff,
    "network":              RetryWithBackoff,
    "api":                  Pause,
    "loop":                 Pause,
    "agent_output_missing": RetryNow,
    "agent_incomplete":     Pause,
    "terminated":           Pause,
    "unknown":              Pause,
}

def decide(
    category: Category,
    *,
    prior_backoff_attempts: int,
    hint_delay_seconds: float | None = None,
) -> ErrorDecision:
    """Map a category + prior-attempts count to a decision.

    RetryWithBackoff categories retry FOREVER (§10): delay = 1·2^n, unbounded.
    There is no attempt ceiling here — interactive runs rely on an interruptible
    sleep + Ctrl-C, and headless runs rely on a wall-clock ceiling in the executor.
    `hint_delay_seconds` (a server Retry-After) is honored as max(hint, our delay)
    but capped at 60s so a misbehaving upstream header can't strand us; that 60s
    cap is on the *hint only*, not on our own doubling.
    """
    cls = _POLICY.get(category, Pause)
    if cls is Pause:
        return Pause()
    if cls is RetryNow:
        return RetryNow()
    delay = 1.0 * (2 ** prior_backoff_attempts)
    if hint_delay_seconds is not None:
        delay = min(60.0, max(delay, hint_delay_seconds))
    return RetryWithBackoff(delay)
