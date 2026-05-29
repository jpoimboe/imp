# tests/test_tui_input.py
from __future__ import annotations

import asyncio

import pytest

from imp.cli import narrative as nv
from imp.cli.prompt import ESC_SENTINEL
from imp.cli.tui import NarrativeApp


def _tree():
    return nv.build_tree([{"id": "1", "agent_id": None, "type": "change_created",
                           "payload": {"change_id": 1, "title": "t"}}])


def test_submitting_a_line_resolves_the_request_future() -> None:
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        fut = asyncio.ensure_future(app.request_line("> "))
        await asyncio.sleep(0)              # let request_line install the future
        app._submit_input("hello there")
        assert await fut == "hello there"
        assert app._input_buffer_text() == ""   # buffer cleared after submit
    asyncio.run(go())


def test_escape_resolves_future_with_esc_sentinel() -> None:
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        fut = asyncio.ensure_future(app.request_line("> "))
        await asyncio.sleep(0)
        app._escape_input()
        assert await fut == ESC_SENTINEL
    asyncio.run(go())


def test_user_chat_input_fn_takes_a_question_and_returns_awaitable() -> None:
    async def go():
        app = NarrativeApp(_tree(), live=True, mouse=False)
        awaitable = app.user_chat_input_fn("anything?")
        fut = asyncio.ensure_future(awaitable)
        await asyncio.sleep(0)
        app._submit_input("yes")
        assert await fut == "yes"
    asyncio.run(go())
