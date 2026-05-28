"""Per-category executor policy for provider errors.

Pure functions + data. No I/O. Consumed by ``imp.engine.executor`` to
decide between Pause / RetryNow / RetryWithBackoff for each
provider-emitted ``error`` event.

The retry-with-backoff categories use a separate attempt counter from
``step.max_retries`` — a flaky network shouldn't burn through the
agent's "you have 3 chances to produce valid output" budget. Counter
lives in local executor variables; see ``_execute_step`` /
``_run_agent_gate`` in ``executor.py``.
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


_POLICY: dict[Category, type] = {
    "auth":                 Pause,
    "config":               Pause,
    "quota":                Pause,
    "content_filter":       Pause,
    "context_window":       Pause,
    "rate_limit":           RetryWithBackoff,
    "network":              RetryWithBackoff,
    "agent_output_missing": RetryNow,
    "unknown":              Pause,
}

_MAX_BACKOFF_ATTEMPTS = 4
_BACKOFF_SCHEDULE = [1.0, 2.0, 4.0, 8.0]


def decide(
    category: Category,
    *,
    prior_backoff_attempts: int,
    hint_delay_seconds: float | None = None,
) -> ErrorDecision:
    """Map a category + prior-attempts count to a concrete decision.

    ``hint_delay_seconds`` carries an optional server-provided delay
    (e.g. Retry-After header from a 429). When set on a
    RetryWithBackoff category, we use ``max(hint, schedule_entry)`` so
    we respect the server's wish without falling below our own
    minimum backoff. Capped at 60s to prevent unbounded sleeps from a
    misbehaving upstream.
    """
    cls = _POLICY.get(category, Pause)
    if cls is Pause:
        return Pause()
    if cls is RetryNow:
        return RetryNow()
    if prior_backoff_attempts >= _MAX_BACKOFF_ATTEMPTS:
        return Pause()
    delay = _BACKOFF_SCHEDULE[prior_backoff_attempts]
    if hint_delay_seconds is not None:
        delay = min(60.0, max(delay, hint_delay_seconds))
    return RetryWithBackoff(delay)
