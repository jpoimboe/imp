from __future__ import annotations

import functools
import time
from pathlib import Path

import anyio
import pytest

from imp.remote.daemon.discover import socket_is_live, socket_path
from imp.remote.daemon.protocol import make_hello
from imp.remote.daemon.runner import pid_path, run_daemon
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeAdapter:
    """A north adapter that records relayed events and never polls real HTTP."""

    def __init__(self) -> None:
        self.events: list[dict] = []

    def on_event(self, event: dict) -> None:
        self.events.append(event)

    def on_response(self, response: object) -> None:
        pass

    def poll_once(self) -> None:
        time.sleep(0.01)   # pace the worker loop; no inbound to submit


@pytest.mark.anyio
async def test_run_daemon_binds_relays_a_south_event_and_cleans_up(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    adapter = FakeAdapter()
    shutdown = anyio.Event()
    spath = socket_path(tmp_path)

    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(
            run_daemon, tmp_path, env={},
            make_adapter=lambda env, submit: adapter,
            install_signals=False, shutdown=shutdown))

        with anyio.fail_after(5):              # socket comes up
            while not socket_is_live(spath):
                await anyio.sleep(0.02)
        assert pid_path(tmp_path).exists()

        # A connector dials, sends its hello, then one event — it must reach the adapter.
        client = await anyio.connect_unix(str(spath))
        await client.send((make_hello("2026-06-13-abc", "T") + "\n").encode())
        await client.send(
            b'{"type": "agent_output", "change": {"id": "2026-06-13-abc"}}\n')
        with anyio.fail_after(5):
            while not adapter.events:
                await anyio.sleep(0.02)
        assert adapter.events[0]["type"] == "agent_output"
        await client.aclose()

        shutdown.set()                          # clean drain

    # After run_daemon returns the socket is unbound and the pidfile removed.
    assert not socket_is_live(spath)
    assert not pid_path(tmp_path).exists()
