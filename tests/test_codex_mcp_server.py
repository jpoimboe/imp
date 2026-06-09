from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from imp.agents.tools import Tool
from imp.agents.tools.context import ToolContext
from imp.providers.codex_mcp import RunState, start_server


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
        subagent_defs={},
        provider_name="codex",
        model_name="gpt-5.5",
        parent_transcript_dir=tmp_path,
    )


def _echo_tool() -> Tool:
    def _fn(ctx, text):
        return f"echo:{text}"
    return Tool(name="echo", description="echo",
                input_schema={"type": "object",
                              "properties": {"text": {"type": "string"}},
                              "required": ["text"]}, fn=_fn)


@pytest.mark.anyio
async def test_server_binds_ephemeral_port_and_returns_url(tmp_path: Path) -> None:
    state = RunState(cap=10)
    handle = await start_server(
        tools={"echo": _echo_tool()}, ctx=_ctx(tmp_path), state=state,
        bearer_token="secret-token-123", host="127.0.0.1",
    )
    try:
        # Returns a real listening port (NOT zero).
        assert handle.port > 0
        assert handle.url.startswith("http://127.0.0.1:")
        # Server task is running.
        assert handle.task is not None and not handle.task.done()
    finally:
        await handle.shutdown()


@pytest.mark.anyio
async def test_server_rejects_request_without_bearer_token(tmp_path: Path) -> None:
    state = RunState(cap=10)
    handle = await start_server(
        tools={"echo": _echo_tool()}, ctx=_ctx(tmp_path), state=state,
        bearer_token="t-correct", host="127.0.0.1",
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(handle.url, json={"jsonrpc": "2.0", "id": 1,
                                                    "method": "initialize",
                                                    "params": {}})
            # No bearer → 401 Unauthorized.
            assert r.status_code == 401
    finally:
        await handle.shutdown()


@pytest.mark.anyio
async def test_server_rejects_request_with_wrong_bearer(tmp_path: Path) -> None:
    state = RunState(cap=10)
    handle = await start_server(
        tools={"echo": _echo_tool()}, ctx=_ctx(tmp_path), state=state,
        bearer_token="t-correct", host="127.0.0.1",
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.url, json={"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": {}},
                headers={"Authorization": "Bearer t-wrong"})
            assert r.status_code == 401
    finally:
        await handle.shutdown()


@pytest.mark.anyio
async def test_server_accepts_request_with_correct_bearer(tmp_path: Path) -> None:
    state = RunState(cap=10)
    handle = await start_server(
        tools={"echo": _echo_tool()}, ctx=_ctx(tmp_path), state=state,
        bearer_token="t-good", host="127.0.0.1",
    )
    try:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                handle.url, json={"jsonrpc": "2.0", "id": 1,
                                  "method": "initialize", "params": {}},
                headers={"Authorization": "Bearer t-good"})
            assert r.status_code != 401 and r.status_code != 403
    finally:
        await handle.shutdown()
