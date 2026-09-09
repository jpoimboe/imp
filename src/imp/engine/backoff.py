# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Shared backoff helper (§10/§12).

Extracted from executor.py so that both executor and subagents can use
interruptible_sleep without creating an import cycle.

No-cycle invariant: this module imports only anyio and stdlib — it must
never import from imp.engine.executor, imp.agents.*, or imp.providers.*.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio


async def interruptible_sleep(delay: float, coordinator: Any, *,
                              tick: float = 0.1,
                              wake_when: Callable[[], bool] | None = None) -> None:
    """Sleep up to `delay`, returning early (within one `tick`) if the
    coordinator requests a pause (§10) or a live retry-now (`r` keypress)
    during a backoff wait, or if the caller's own `wake_when` predicate turns
    true. Without this, a long unbounded backoff would swallow Ctrl-C — and the
    retry-now gesture — until it woke. `wake_when` is per-call-site on purpose:
    callers that cannot act on the condition must not wake on it."""
    remaining = delay
    while remaining > 0:
        if coordinator is not None:
            if getattr(coordinator, "should_pause", False):
                return
            if getattr(coordinator, "retry_now_requested", False):
                return
        if wake_when is not None and wake_when():
            return
        step = min(tick, remaining)
        await anyio.sleep(step)
        remaining -= step
