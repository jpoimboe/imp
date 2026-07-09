from __future__ import annotations

import anyio
import pytest

from imp.agents.tools.context import request_bracketed
from imp.cli.signals import InterruptCoordinator


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _Source:
    """Async source that records coordinator.request_active at the moment it is
    about to produce each item — i.e. while the consumer is blocked inside
    request_bracketed's guarded `await it.__anext__()`."""

    def __init__(self, items: list[int], coord: InterruptCoordinator,
                 wait_snap: list[bool]) -> None:
        self._items = list(items)
        self._i = 0
        self._coord = coord
        self._wait_snap = wait_snap

    def __aiter__(self) -> _Source:
        return self

    async def __anext__(self) -> int:
        self._wait_snap.append(self._coord.request_active)
        if self._i >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._i]
        self._i += 1
        return item


@pytest.mark.anyio
async def test_holds_state_during_wait_and_releases_before_yield() -> None:
    coord = InterruptCoordinator()
    wait_snap: list[bool] = []
    yield_snap: list[bool] = []
    got: list[int] = []
    async for item in request_bracketed(coord, _Source([1, 2, 3], coord, wait_snap)):
        yield_snap.append(coord.request_active)
        got.append(item)

    assert got == [1, 2, 3]
    # Armed while awaiting each of the 3 items AND the final StopAsyncIteration
    # pull: 4 True snapshots.
    assert wait_snap == [True, True, True, True]
    # Released before every yield, so consumer-side processing runs under the
    # normal window.
    assert yield_snap == [False, False, False]
    # Balanced after exhaustion — no depth leak.
    assert coord.request_active is False


@pytest.mark.anyio
async def test_releases_on_consumer_exception() -> None:
    coord = InterruptCoordinator()
    src = _Source([1, 2, 3], coord, [])
    with pytest.raises(RuntimeError):
        async for _item in request_bracketed(coord, src):
            raise RuntimeError("boom")
    # Depth was released before the yield the consumer raised out of.
    assert coord.request_active is False


@pytest.mark.anyio
async def test_releases_on_consumer_break() -> None:
    coord = InterruptCoordinator()
    src = _Source([1, 2, 3], coord, [])
    got: list[int] = []
    async for item in request_bracketed(coord, src):
        got.append(item)
        break
    assert got == [1]
    # Depth was released before the yield the consumer broke out of.
    assert coord.request_active is False


@pytest.mark.anyio
async def test_releases_when_source_raises() -> None:
    coord = InterruptCoordinator()

    class _Boom:
        def __aiter__(self) -> _Boom:
            return self

        async def __anext__(self) -> int:
            raise ValueError("source failed")

    with pytest.raises(ValueError):
        async for _item in request_bracketed(coord, _Boom()):
            pass  # pragma: no cover
    assert coord.request_active is False


@pytest.mark.anyio
async def test_releases_on_cancellation_during_wait() -> None:
    coord = InterruptCoordinator()

    class _Hang:
        def __aiter__(self) -> _Hang:
            return self

        async def __anext__(self) -> int:
            await anyio.sleep_forever()

    with anyio.move_on_after(0.05):
        async for _item in request_bracketed(coord, _Hang()):
            pass  # pragma: no cover
    # Cancellation hit the guarded await; the finally released the depth.
    assert coord.request_active is False


@pytest.mark.anyio
async def test_none_coordinator_passes_through() -> None:
    async def _plain() -> object:
        yield 1
        yield 2

    got = [item async for item in request_bracketed(None, _plain())]
    assert got == [1, 2]


@pytest.mark.anyio
async def test_partial_coordinator_lacking_methods_passes_through() -> None:
    class _PartialCoord:
        """A coordinator double that implements NONE of the request-watchdog
        methods — request_bracketed must degrade to a plain pass-through."""

    async def _plain() -> object:
        yield 1
        yield 2

    got = [item async for item in request_bracketed(_PartialCoord(), _plain())]
    assert got == [1, 2]
