from __future__ import annotations

import asyncio
import contextlib

import pytest

from imp.providers.codex_runner import race_consume_against_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_dead_server_task_first_classifies_network() -> None:
    async def _slow_consume() -> str:
        await asyncio.sleep(5.0)
        return "consume-done"

    async def _server_dies() -> None:
        await asyncio.sleep(0.05)
        raise RuntimeError("uvicorn fell over")

    consume = asyncio.create_task(_slow_consume())
    server = asyncio.create_task(_server_dies())
    try:
        outcome = await race_consume_against_server(consume, server,
                                                    timeout=2.0)
        assert outcome.kind == "server_died"
        assert "uvicorn fell over" in outcome.detail
    finally:
        consume.cancel()
        # Best-effort drain.
        for t in (consume, server):
            with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
                await asyncio.wait_for(t, timeout=0.1)


@pytest.mark.anyio
async def test_consume_finishes_first_returns_completed() -> None:
    async def _fast_consume() -> str:
        await asyncio.sleep(0.05)
        return "done"

    async def _server_runs() -> None:
        await asyncio.sleep(5.0)

    consume = asyncio.create_task(_fast_consume())
    server = asyncio.create_task(_server_runs())
    try:
        outcome = await race_consume_against_server(consume, server,
                                                    timeout=2.0)
        assert outcome.kind == "consume_done"
    finally:
        server.cancel()
        with contextlib.suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(server, timeout=0.1)
