from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli.mutation_proxy import build_session_handler
from imp.cli.store import LocalStore
from imp.cli.store_proxy import ProxyStore
from imp.engine.quest import Quest
from imp.remote.session.server import SessionServer
from tests.async_wait import wait_until
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _append_event(quest: Quest, event: dict) -> None:
    with quest.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


@pytest.mark.anyio
async def test_build_session_handler_reads_off_the_event_loop(tmp_path: Path) -> None:
    # The handler must not block the loop: assert it hands the blocking store
    # call to a worker thread (a different thread id than the loop thread).
    import threading

    _seed_control(tmp_path)
    Quest.create(tmp_path, title="Q", quest_id="q1", base_branch="main")
    handler = build_session_handler(tmp_path)
    loop_thread = threading.get_ident()

    seen: dict[str, int] = {}

    import imp.cli.store_proxy as sp

    orig_run_query = sp.run_query   # capture before patching to avoid recursion

    def _probe(store: Any, op: str, args: dict) -> Any:
        seen["thread"] = threading.get_ident()
        return orig_run_query(store, op, args)

    import unittest.mock as mock
    with mock.patch.object(sp, "run_query", _probe):
        result = await handler("completed_rows", {})
    assert result == []
    assert seen["thread"] != loop_thread   # ran on a worker thread, not the loop


@pytest.mark.anyio
async def test_query_is_served_while_run_fn_is_parked(tmp_path: Path) -> None:
    # End-to-end: a query is served over the real socket while run_fn is parked
    # (integration wiring; the off-loop invariant itself is pinned by the
    # thread-id assertion in the sibling test).
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Live", quest_id="liveq", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"subtype": "reply", "content": [{"type": "text", "text": "hi"}]}})

    sock = str(tmp_path / "s.sock")
    release = anyio.Event()
    server = SessionServer(sock, on_query=build_session_handler(tmp_path))

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await release.wait()          # park run_fn (cooperative wait; loop stays free)
        return "done"

    async with anyio.create_task_group() as tg:
        await server.open()
        result: dict[str, Any] = {}

        async def _serve() -> None:
            result["r"] = await server.serve(run_fn)

        tg.start_soon(_serve)
        proxy = ProxyStore(sock)
        events = await anyio.to_thread.run_sync(proxy.get_events, "liveq")
        assert events == LocalStore(tmp_path).get_events("liveq")   # served while parked
        release.set()
        await wait_until(lambda: "r" in result)
        assert result["r"] == "done"
        tg.cancel_scope.cancel()
