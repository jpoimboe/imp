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
async def test_start_spawns_and_feeds_events(tmp_path: Path) -> None:
    log = tmp_path / "events.log"
    ch = _channel(fake_driver_command(tmp_path, log=log))
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is True
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
async def test_start_drains_stderr_so_driver_stays_unblocked(tmp_path: Path) -> None:
    # The driver floods 200KiB to stderr (past a pipe buffer) BEFORE reading any
    # stdin. A captured-but-undrained stderr pipe would wedge the driver there, so
    # the event we feed below would never be logged. Captured + drained → the driver
    # gets past the flood, reads stdin, and logs the event.
    log = tmp_path / "events.log"
    ch = _channel(fake_driver_command(tmp_path, log=log, stderr_bytes=200_000))
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is True
        assert ch._proc.stderr is not None       # captured, not inherited onto the tty
        ch.on_event({"id": "e1", "type": "agent_output", "payload": {"k": "v"}})
        with anyio.fail_after(5):
            while not (log.exists() and log.read_text().strip()):
                await anyio.sleep(0.02)
        assert json.loads(log.read_text().splitlines()[0])["type"] == "agent_output"
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
async def test_start_degrades_silently_when_command_missing(tmp_path: Path) -> None:
    # A configured-but-absent notifier binary must NOT print an error banner on
    # every run: start returns False (no notifications) quietly.
    errors: list[str] = []
    ch = RemoteChannel(
        command=("/no/such/driver/binary",), events=("all",), startup_timeout=2.0,
        reply_timeout=None, id="2026-06-11-abc", title="t",
        on_error=errors.append)
    async with anyio.create_task_group() as tg:
        assert await ch.start(tg) is False
        assert errors == []                  # degraded quietly, no spawn banner
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_start_emits_error_on_permission_denied(tmp_path: Path) -> None:
    from unittest import mock

    errors: list[str] = []
    ch = RemoteChannel(
        command=("/some/driver",), events=("all",), startup_timeout=2.0,
        reply_timeout=None, id="2026-06-11-abc", title="t",
        on_error=errors.append)
    with mock.patch("subprocess.Popen", side_effect=PermissionError("denied")):
        async with anyio.create_task_group() as tg:
            assert await ch.start(tg) is False
            assert any("spawn" in m for m in errors)   # banner emitted
            tg.cancel_scope.cancel()
