from __future__ import annotations

from collections.abc import Callable

import anyio

# anyio timers fire on wall-clock; inner windows false-fire under -n auto before the 240s outer cap.
HANG_BACKSTOP = 120.0


async def wait_until(
    predicate: Callable[[], bool], *, backstop: float = HANG_BACKSTOP, interval: float = 0.02,
) -> None:
    """Poll `predicate` until true; `backstop` is a hang-detector only, never a tuned window."""
    with anyio.fail_after(backstop):
        while not predicate():
            await anyio.sleep(interval)
