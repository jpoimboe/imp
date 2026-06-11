# tests/test_remote_channel_lifecycle.py
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from imp.cli.remote_channel import RemoteChannel
from tests._remote_fakes import fake_driver_command


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _channel(cmd: list[str]) -> RemoteChannel:
    return RemoteChannel(
        command=tuple(cmd), events=("all",), startup_timeout=2.0,
        reply_timeout=None, id="2026-06-11-abc", title="t")


@pytest.mark.anyio
async def test_start_spawns_feeds_events_and_reads_reply(tmp_path: Path) -> None:
    log = tmp_path / "events.log"
    ch = _channel(fake_driver_command(tmp_path, log=log, reply="do the thing"))
    async with anyio.create_task_group() as tg:
        up = await ch.start(tg)
        assert up is True
        # The driver's startup reply is read back through next_reply.
        with anyio.fail_after(5):
            assert await ch.next_reply() == "do the thing"
        # An allowlisted event reaches the driver and is logged (NDJSON framed).
        ch.on_event({"id": "e1", "type": "agent_output", "payload": {"k": "v"}})
        with anyio.fail_after(5):
            while not (log.exists() and log.read_text().strip()):
                await anyio.sleep(0.02)
        logged = json.loads(log.read_text().splitlines()[0])
        assert logged["type"] == "agent_output"
        assert logged["change"]["id"] == "2026-06-11-abc"
        await ch.stop()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_start_returns_false_when_driver_exits_immediately(
        tmp_path: Path) -> None:
    errors: list[str] = []
    ch = RemoteChannel(
        command=tuple(fake_driver_command(tmp_path, exit_immediately=True)),
        events=("all",), startup_timeout=2.0, reply_timeout=None,
        id="2026-06-11-abc", title="t", on_error=errors.append)
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is False
        assert any("startup" in m for m in errors)   # logged, run continues
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_start_returns_false_when_command_missing(tmp_path: Path) -> None:
    errors: list[str] = []
    ch = RemoteChannel(
        command=("/no/such/driver/binary",), events=("all",), startup_timeout=2.0,
        reply_timeout=None, id="2026-06-11-abc", title="t",
        on_error=errors.append)
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is False
        assert any("spawn" in m for m in errors)
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_next_reply_timeout_raises_awaiting_user_input(tmp_path: Path) -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput
    ch = _channel(fake_driver_command(tmp_path))  # no reply configured
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is True
        with pytest.raises(AwaitingUserInput):
            with anyio.fail_after(5):
                await ch.next_reply(timeout=0.05)  # short timeout, no reply → raise
        await ch.stop()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_next_reply_raises_when_driver_dies(tmp_path: Path) -> None:
    from imp.agents.tools.wait_for_user import AwaitingUserInput
    # Driver sends one reply then exits (no more stdin to read from → EOF on stdout)
    ch = _channel(fake_driver_command(tmp_path, reply="only-one"))
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is True
        with anyio.fail_after(5):
            assert await ch.next_reply() == "only-one"
        # Driver process exits naturally after receiving EOF on its stdin.
        # next_reply on a dead/closed channel should raise AwaitingUserInput.
        await ch.stop()
        with anyio.fail_after(5):
            with pytest.raises(AwaitingUserInput):
                await ch.next_reply()
        tg.cancel_scope.cancel()
