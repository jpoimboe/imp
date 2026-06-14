# tests/test_remote_reply_select.py
from __future__ import annotations

import anyio
import pytest

from imp.cli.remote_channel import compose_on_event, effective_reply_fn


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_compose_on_event_fans_out_to_both() -> None:
    a: list[object] = []
    b: list[object] = []
    cb = compose_on_event(a.append, b.append)
    cb({"type": "x"})
    assert a == [{"type": "x"}] and b == [{"type": "x"}]


@pytest.mark.anyio
async def test_no_channel_returns_reply_fn_unchanged() -> None:
    async def local() -> str:
        return "tty"
    fn = effective_reply_fn(local, None, non_interactive=False)
    assert fn is local


class _FakeChannel:
    reply_timeout = None

    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.timeout_seen: object = "unset"

    async def next_reply(self, *, timeout: float | None = None) -> str:
        self.timeout_seen = timeout
        return self._reply


@pytest.mark.anyio
async def test_phone_only_falls_through_to_phone_when_no_local_seed() -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    async def local_pause() -> str:
        raise AwaitingUserInput()

    ch = _FakeChannel("from-phone")
    ch.reply_timeout = 30.0  # type: ignore[assignment]
    fn = effective_reply_fn(local_pause, ch, non_interactive=True)
    assert await fn() == "from-phone"
    assert ch.timeout_seen == 30.0   # phone-only honors reply_timeout


@pytest.mark.anyio
async def test_phone_only_returns_local_seed_before_phone() -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    delivered = False

    async def local_seed() -> str:
        nonlocal delivered
        if delivered:
            raise AwaitingUserInput()
        delivered = True
        return "seed-intent"

    ch = _FakeChannel("from-phone")
    ch.reply_timeout = 30.0  # type: ignore[assignment]
    fn = effective_reply_fn(local_seed, ch, non_interactive=True)

    assert await fn() == "seed-intent"   # first solicit: the local seed wins
    assert ch.timeout_seen == "unset"    # phone was NOT consulted yet
    assert await fn() == "from-phone"    # seed exhausted → fall through to phone
    assert ch.timeout_seen == 30.0       # phone-only still honors reply_timeout


@pytest.mark.anyio
async def test_attended_races_local_against_driver_driver_wins() -> None:
    ch = _FakeChannel("from-driver")
    fn = effective_reply_fn(_local_never, ch, non_interactive=False)
    with anyio.fail_after(5):
        assert await fn() == "from-driver"


@pytest.mark.anyio
async def test_attended_race_dead_channel_yields_to_local() -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput

    class _DeadChannel:
        reply_timeout = None

        async def next_reply(self, *, timeout: float | None = None) -> str:
            raise AwaitingUserInput()

    async def local() -> str:
        return "from-tty"

    fn = effective_reply_fn(local, _DeadChannel(), non_interactive=False)
    with anyio.fail_after(5):
        assert await fn() == "from-tty"   # dead channel never wins the race



async def _local_never() -> str:
    await anyio.sleep_forever()
    raise AssertionError("unreachable")
