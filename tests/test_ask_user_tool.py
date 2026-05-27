# tests/test_ask_user_tool.py
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import imp.agents.tools.ask_user  # noqa: F401
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext


def _ctx(tmp_path: Path, input_fn=None) -> ToolContext:
    artifacts = tmp_path / "_artifacts"
    artifacts.mkdir()
    ctx = ToolContext(
        project_root=tmp_path, artifacts_dir=artifacts,
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )
    ctx.user_input_fn = input_fn
    return ctx


def test_ask_user_returns_user_input_sync(tmp_path: Path) -> None:
    """user_input_fn may be a sync callable that returns str directly."""
    ctx = _ctx(tmp_path, input_fn=lambda prompt: "the answer")
    out = asyncio.run(
        get_registry()["ask_user"].async_invoke(ctx, {"question": "what?"})
    )
    assert out == "the answer"


def test_ask_user_returns_user_input_async(tmp_path: Path) -> None:
    """user_input_fn may be a sync callable that returns an awaitable
    (e.g. the CLI's prompt_toolkit ask_async); ask_user awaits it."""
    async def _async_input(_prompt: str) -> str:
        return "the awaited answer"

    ctx = _ctx(tmp_path, input_fn=_async_input)
    out = asyncio.run(
        get_registry()["ask_user"].async_invoke(ctx, {"question": "what?"})
    )
    assert out == "the awaited answer"


def test_ask_user_without_handler_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, input_fn=None)
    with pytest.raises(RuntimeError, match="not available"):
        asyncio.run(
            get_registry()["ask_user"].async_invoke(ctx, {"question": "x"})
        )
