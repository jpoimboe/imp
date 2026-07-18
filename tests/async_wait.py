from __future__ import annotations

from collections.abc import Callable

import anyio


async def wait_until(
    predicate: Callable[[], bool], *, backstop: float = 30.0, interval: float = 0.02,
) -> None:
    """Poll `predicate` until true; `backstop` is a hang-detector only, never a tuned window."""
    with anyio.fail_after(backstop):
        while not predicate():
            await anyio.sleep(interval)
