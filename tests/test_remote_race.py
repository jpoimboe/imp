# tests/test_remote_race.py
from __future__ import annotations

import anyio
import pytest

from imp.cli.remote_channel import RaceResult, race_reply


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_local_wins_and_remote_is_cancelled() -> None:
    remote_cancelled = False

    async def local() -> str:
        return "from-tty"

    async def remote() -> str:
        nonlocal remote_cancelled
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            remote_cancelled = True
            raise
        return "never"

    result = await race_reply(local, remote)
    assert result == RaceResult(source="local", value="from-tty")
    assert remote_cancelled is True


@pytest.mark.anyio
async def test_remote_wins_and_local_is_cancelled() -> None:
    local_cancelled = False

    async def local() -> str:
        nonlocal local_cancelled
        try:
            await anyio.sleep_forever()
        except anyio.get_cancelled_exc_class():
            local_cancelled = True
            raise
        return "never"

    async def remote() -> str:
        return "from-driver"

    result = await race_reply(local, remote)
    assert result == RaceResult(source="remote", value="from-driver")
    assert local_cancelled is True


@pytest.mark.anyio
async def test_race_reply_unwraps_eoferror_from_local() -> None:
    """EOFError (Ctrl-D) from the local prompt must propagate BARE so the
    caller's `except EOFError:` arm catches it (not a BaseExceptionGroup)."""
    async def _local() -> str:
        raise EOFError

    async def _remote() -> str:
        await anyio.sleep(10)
        return "remote"

    with pytest.raises(EOFError):
        await race_reply(_local, _remote)


# KeyboardInterrupt shares the identical unwrap path as EOFError above.  Raising
# a real KBI from a coroutine is hostile to the asyncio test runner (Task.__step
# re-raises it before anyio can wrap it), so that path is not covered here —
# the same policy as test_prompt_teardown_on_exit.py:124-127.
