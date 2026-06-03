"""Tests for §10 executor backoff helpers: interruptible sleep + headless ceiling."""

from __future__ import annotations

import anyio

from imp.engine.backoff import interruptible_sleep as _interruptible_sleep


class _Coord:
    def __init__(self) -> None:
        self.should_pause = False


def test_interruptible_sleep_returns_within_a_tick_on_pause() -> None:
    coord = _Coord()

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            async def flip() -> None:
                await anyio.sleep(0.05)
                coord.should_pause = True
            tg.start_soon(flip)
            # a 10s sleep must abandon well before 10s once should_pause flips
            with anyio.fail_after(1.0):
                await _interruptible_sleep(10.0, coord, tick=0.02)

    anyio.run(main)


def test_interruptible_sleep_with_none_coordinator() -> None:
    """coordinator=None branch: the sleep must complete without raising.
    (The loop skips the should_pause check and just runs out the delay.)"""
    async def main() -> None:
        with anyio.fail_after(2.0):
            await _interruptible_sleep(0.05, None, tick=0.02)

    anyio.run(main)
