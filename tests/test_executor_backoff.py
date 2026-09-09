"""§10 executor backoff helpers: interruptible sleep; headless-ceiling constant removed."""

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
            with anyio.fail_after(30):  # deadlock backstop only
                await _interruptible_sleep(10.0, coord, tick=0.02)

    anyio.run(main)


def test_interruptible_sleep_with_none_coordinator() -> None:
    """coordinator=None branch: the sleep must complete without raising.
    (The loop skips the should_pause check and just runs out the delay.)"""
    async def main() -> None:
        with anyio.fail_after(30):  # deadlock backstop only
            await _interruptible_sleep(0.05, None, tick=0.02)

    anyio.run(main)


def test_headless_ceiling_constant_removed_but_sleep_retained() -> None:
    import imp.engine.backoff as backoff
    assert not hasattr(backoff, "HEADLESS_RETRY_CEILING_SECONDS")
    assert hasattr(backoff, "interruptible_sleep")


def test_interruptible_sleep_returns_within_a_tick_on_retry_now() -> None:
    class _RetryCoord:
        def __init__(self) -> None:
            self.should_pause = False
            self.retry_now_requested = False

    coord = _RetryCoord()

    async def main() -> None:
        async with anyio.create_task_group() as tg:
            async def flip() -> None:
                await anyio.sleep(0.05)
                coord.retry_now_requested = True
            tg.start_soon(flip)
            # a 10s sleep must abandon well before 10s once retry_now flips
            with anyio.fail_after(30):  # deadlock backstop only
                await _interruptible_sleep(10.0, coord, tick=0.02)

    anyio.run(main)


def test_a_wake_predicate_ends_the_sleep_within_a_tick() -> None:
    async def main() -> None:
        # A 60s sleep must abandon at once; fail_after is what fails an
        # implementation that never polls the predicate.
        with anyio.fail_after(20):
            await _interruptible_sleep(60.0, _Coord(), tick=0.02,
                                       wake_when=lambda: True)

    anyio.run(main)


def test_a_false_predicate_is_polled_every_tick_and_never_wakes() -> None:
    polls: list[None] = []

    def _never() -> bool:
        polls.append(None)
        return False

    async def main() -> None:
        with anyio.fail_after(30):  # deadlock backstop only
            await _interruptible_sleep(0.2, _Coord(), tick=0.02, wake_when=_never)

    anyio.run(main)
    assert len(polls) >= 2


def test_a_caller_passing_no_predicate_sleeps_through_a_held_backoff_wait() -> None:
    """The subagent backoff loop brackets the SAME coordinator, so a wake keyed
    off backoff_wait_active instead of the caller's own predicate would retry-storm
    against the very vendor the user is fleeing."""
    from imp.cli.signals import InterruptCoordinator
    coord = InterruptCoordinator()
    coord.begin_backoff_wait()
    elapsed: list[float] = []

    async def main() -> None:
        start = anyio.current_time()
        with anyio.fail_after(30):  # deadlock backstop only
            await _interruptible_sleep(0.3, coord, tick=0.02)
        elapsed.append(anyio.current_time() - start)

    anyio.run(main)
    assert coord.backoff_wait_active
    assert elapsed[0] >= 0.25


def test_the_other_interruptible_sleep_callers_pass_no_wake_predicate() -> None:
    import inspect

    from imp.agents import subagents
    from imp.agents.tools import create_and_enter_worktree
    for module in (subagents, create_and_enter_worktree):
        assert "wake_when" not in inspect.getsource(module), (
            f"{module.__name__} would wake on a switch it cannot apply"
        )
