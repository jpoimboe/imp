"""Tests for the claude_code session-isolation subprocess patch.

Covers:
- _SessionIsolatingAnyio proxy: injects start_new_session, respects explicit value, forwards attrs.
- install_claude_session_isolation: idempotent, replaces the SDK module ref, forwards real attrs.
- Real grouping: a child spawned through the proxy lands in its own process group.
"""
from __future__ import annotations

import os
import sys
from typing import Any

import pytest

from imp.providers.claude_code_session import (
    _SessionIsolatingAnyio,
    install_claude_session_isolation,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_proxy_injects_session_isolation_and_forwards() -> None:
    """_SessionIsolatingAnyio injects start_new_session=True when absent, respects
    an explicit value, and forwards other attributes unchanged."""
    calls: list[dict[str, Any]] = []

    class _FakeAnyio:
        SENTINEL = 11

        async def open_process(self, *a: Any, **k: Any) -> str:
            calls.append(k)
            return "process"

    proxy = _SessionIsolatingAnyio(_FakeAnyio())

    await proxy.open_process(["prog"])
    assert calls[-1]["start_new_session"] is True, "session isolation should be injected"

    await proxy.open_process(["prog"], start_new_session=False)
    assert calls[-1]["start_new_session"] is False, "explicit flag must win over default"

    assert proxy.SENTINEL == 11, "non-overridden attributes should forward unchanged"


def test_install_is_idempotent_and_forwards() -> None:
    """install_claude_session_isolation() is idempotent; the proxy forwards real anyio attrs."""
    import anyio
    import claude_agent_sdk._internal.transport.subprocess_cli as subprocess_cli

    original = subprocess_cli.anyio
    try:
        install_claude_session_isolation()
        install_claude_session_isolation()  # second call must be a no-op
        assert isinstance(subprocess_cli.anyio, _SessionIsolatingAnyio)
        assert subprocess_cli.anyio.fail_after is anyio.fail_after
    finally:
        subprocess_cli.anyio = original  # restore so the patch doesn't leak


@pytest.mark.anyio
async def test_proxy_spawns_child_in_its_own_process_group() -> None:
    """Through the real proxy, open_process spawns a session-isolated child:
    its process-group id differs from imp's own."""
    import anyio

    proxy = _SessionIsolatingAnyio(anyio)
    proc = await proxy.open_process(
        [sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert os.getpgid(proc.pid) != os.getpgid(0)
    finally:
        proc.kill()
        await proc.wait()
