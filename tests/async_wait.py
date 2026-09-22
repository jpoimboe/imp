from __future__ import annotations

import os
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


async def wait_pgid_gone(pgid: int, *, backstop: float = HANG_BACKSTOP) -> None:
    """Poll until the group is unsignalable — a killed group lingers as zombies until reaped."""
    def gone() -> bool:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return True
        return False

    await wait_until(gone, backstop=backstop, interval=0.05)
