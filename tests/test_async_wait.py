from __future__ import annotations

import pytest

from tests.async_wait import wait_until


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_wait_until_polls_until_predicate_true() -> None:
    calls = {"n": 0}

    def predicate() -> bool:
        calls["n"] += 1
        return calls["n"] >= 3

    await wait_until(predicate, interval=0.001)

    assert calls["n"] == 3   # polled until true, then stopped — did not over-poll


@pytest.mark.anyio
async def test_wait_until_returns_immediately_when_already_true() -> None:
    await wait_until(lambda: True, backstop=0.05, interval=0.001)


@pytest.mark.anyio
async def test_wait_until_raises_timeout_when_never_true() -> None:
    with pytest.raises(TimeoutError):
        await wait_until(lambda: False, backstop=0.05, interval=0.001)


def test_hang_backstop_is_120() -> None:
    from tests.async_wait import HANG_BACKSTOP
    assert HANG_BACKSTOP == 120.0


def test_wait_until_defaults_backstop_to_hang_backstop() -> None:
    import inspect

    from tests.async_wait import HANG_BACKSTOP
    default = inspect.signature(wait_until).parameters["backstop"].default
    assert default == HANG_BACKSTOP
