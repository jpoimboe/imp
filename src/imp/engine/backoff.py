"""Shared backoff helpers (§10/§12).

Extracted from executor.py so that both executor and subagents can use
interruptible_sleep and HEADLESS_RETRY_CEILING_SECONDS without creating an
import cycle.

No-cycle invariant: this module imports only anyio and stdlib — it must
never import from imp.engine.executor, imp.agents.*, or imp.providers.*.
"""

from __future__ import annotations

from typing import Any

import anyio

HEADLESS_RETRY_CEILING_SECONDS = 1800.0   # §10/§12: 30-min cumulative backoff


async def interruptible_sleep(delay: float, coordinator: Any, *,
                              tick: float = 0.1) -> None:
    """Sleep up to `delay`, returning early (within one `tick`) if the
    coordinator requests a pause (§10). Without this, a long unbounded backoff
    would swallow Ctrl-C until it woke."""
    remaining = delay
    while remaining > 0:
        if coordinator is not None and getattr(coordinator, "should_pause", False):
            return
        step = min(tick, remaining)
        await anyio.sleep(step)
        remaining -= step
