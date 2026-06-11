# tests/test_remote_channel_teardown.py
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from imp.cli.remote_channel import RemoteChannel
from tests._remote_fakes import fake_driver_command


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _channel(cmd: list[str], errors: list[str]) -> RemoteChannel:
    return RemoteChannel(
        command=tuple(cmd), events=("all",), startup_timeout=2.0,
        reply_timeout=None, id="2026-06-11-abc", title="t",
        on_error=errors.append)


@pytest.mark.anyio
async def test_stop_terminates_a_live_driver(tmp_path: Path) -> None:
    ch = _channel(fake_driver_command(tmp_path), [])
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is True
        proc = ch._proc
        with anyio.fail_after(5):
            await ch.stop()
        assert proc.poll() is not None       # the process is gone
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_stop_kills_sigterm_ignoring_driver(tmp_path: Path) -> None:
    """SIGTERM-ignoring driver: SIGKILL escalation path must terminate it."""
    ch = _channel(fake_driver_command(tmp_path, ignore_sigterm=True), [])
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is True
        proc = ch._proc
        with anyio.fail_after(10):   # grace=2s + margin
            await ch.stop()
        assert proc.poll() is not None   # killed by SIGKILL
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_on_event_after_mid_run_crash_does_not_raise(tmp_path: Path) -> None:
    ch = _channel(fake_driver_command(tmp_path), [])
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is True
        ch._proc.kill()                       # driver dies mid-run
        await anyio.to_thread.run_sync(ch._proc.wait)
        # The event tap survives a dead driver: no raise, run unaffected.
        ch.on_event({"id": "e", "type": "agent_output", "payload": {}})
        await ch.stop()                       # idempotent on an already-dead proc
        tg.cancel_scope.cancel()
