"""Tests for claude's grandchild-reaping helper (_reap_transport_group)."""
from __future__ import annotations

import os
import sys
import tempfile
import time
from typing import Any

import pytest

from imp.providers.claude import _reap_transport_group


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_reap_transport_group_kills_grandchild() -> None:
    """_reap_transport_group killpg's the transport child's group, reaping the
    grandchild sleeper too."""
    import anyio

    class _FakeTransport:
        def __init__(self, process: Any) -> None:
            self._process = process

    class _FakeClient:
        def __init__(self, transport: Any) -> None:
            self._transport = transport

    with tempfile.NamedTemporaryFile(delete=False) as f:
        pidfile = f.name

    # A session-leader child (start_new_session) that spawns a long sleeper
    # grandchild in its OWN process group and records the sleeper's pid.
    code = (
        "import subprocess, sys, time; "
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({pidfile!r}, 'w').write(str(p.pid)); "
        "time.sleep(60)"
    )
    proc = await anyio.open_process(
        [sys.executable, "-c", code], start_new_session=True)

    # Wait until the grandchild pid is recorded.
    sleeper_pid: int | None = None
    deadline = time.time() + 10
    while time.time() < deadline:
        with open(pidfile) as _f:
            txt = _f.read().strip()
        if txt:
            sleeper_pid = int(txt)
            break
        await anyio.sleep(0.05)
    assert sleeper_pid is not None, "child never recorded its grandchild pid"

    client = _FakeClient(_FakeTransport(proc))
    await _reap_transport_group(client)
    await proc.wait()

    # The grandchild sleeper is gone (reparented + reaped after the group kill).
    gone = False
    deadline = time.time() + 5
    while time.time() < deadline:
        try:
            os.kill(sleeper_pid, 0)
        except ProcessLookupError:
            gone = True
            break
        await anyio.sleep(0.05)
    os.unlink(pidfile)
    assert gone, "grandchild sleeper survived the group reap"


@pytest.mark.anyio
async def test_reap_transport_group_noop_when_absent() -> None:
    """Absent client / transport / process is a no-op, no raise."""
    await _reap_transport_group(None)

    class _NoTransport:
        _transport = None

    await _reap_transport_group(_NoTransport())

    class _Transport:
        _process = None

    class _NoProcess:
        _transport = _Transport()

    await _reap_transport_group(_NoProcess())


@pytest.mark.anyio
async def test_reap_transport_group_returns_when_process_ignores_sigkill() -> None:
    """A process whose wait() never completes (D-state, ignores SIGKILL) must not
    wedge the reap: both signals are sent, then the bounded waits give up."""
    import asyncio

    calls: list[str] = []

    class _StuckProcess:
        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        async def wait(self) -> int:
            await asyncio.Event().wait()
            return 0

    class _FakeTransport:
        _process = _StuckProcess()

    class _FakeClient:
        _transport = _FakeTransport()

    await asyncio.wait_for(_reap_transport_group(_FakeClient()), timeout=5)
    assert calls == ["terminate", "kill"]
