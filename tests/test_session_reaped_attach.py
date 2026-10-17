"""An attach processed after a terminal run reaped its quest home is refused, not replayed."""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.engine.state import JsonlEventLog
from imp.remote.session.client import SessionClient, StreamEnding
from imp.remote.session.protocol import encode_attach
from imp.remote.session.server import SessionServer
from tests.async_wait import HANG_BACKSTOP


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _journal_cli_attached(events: Path, calls: list[None]) -> Callable[[], None]:
    def on_first_attach() -> None:   # mirrors the daemon's cli_attached append
        calls.append(None)
        JsonlEventLog(events).append(agent_id=None, type="cli_attached", payload={})
    return on_first_attach


@pytest.mark.anyio
async def test_attach_after_the_reap_is_cut_and_leaves_the_home_gone(tmp_path: Path) -> None:
    events = tmp_path / "home" / "events.jsonl"
    events.parent.mkdir()
    events.write_text(
        json.dumps({"id": "c", "type": "quest_created", "payload": {}}) + "\n")
    sock = str(tmp_path / "s.sock")
    first_attaches: list[None] = []
    server = SessionServer(
        sock, events_path=events,
        on_first_attach=_journal_cli_attached(events, first_attaches))
    settled = anyio.Event()   # the attach was served (old behaviour) or refused
    seen: list[dict[str, Any]] = []

    def on_event(ev: dict[str, Any]) -> None:
        seen.append(ev)
        if ev["type"] == "replay_complete":
            settled.set()

    client = SessionClient(sock, "q1", "T", on_event=on_event, lease=True)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        done = {"id": "w", "type": "workflow_completed", "payload": {"commits": []}}
        with events.open("a") as f:
            f.write(json.dumps(done) + "\n")
        on_event(done)            # broadcast to nobody: the attach is still queued
        events.unlink()           # _archive_and_reap
        events.parent.rmdir()
        await settled.wait()      # the queued attach is processed post-reap, pre-end-frame
        return "completed"

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)
        await client.connect()
        with anyio.fail_after(HANG_BACKSTOP):
            ending = await client.receive_events()
        settled.set()
        await client.aclose()
        tg.cancel_scope.cancel()

    assert ending is StreamEnding.CUT   # not ENDED-with-no-outcome
    assert seen == []                   # no replay boundary, no end frame
    assert first_attaches == []
    assert not events.parent.exists()   # no cli_attached append resurrected the home


@pytest.mark.anyio
async def test_a_refused_attach_runs_no_first_attach_side_effect(tmp_path: Path) -> None:
    events = tmp_path / "home" / "events.jsonl"   # never created: the home is gone
    sock = str(tmp_path / "s.sock")
    first_attaches: list[None] = []
    server = SessionServer(
        sock, events_path=events,
        on_first_attach=_journal_cli_attached(events, first_attaches))
    release = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await release.wait()
        return "ok"

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)
        raw = await anyio.connect_unix(sock)
        await raw.send((encode_attach("q1", "T", lease=True) + "\n").encode())
        with anyio.fail_after(HANG_BACKSTOP), pytest.raises(anyio.EndOfStream):
            await raw.receive()
        assert first_attaches == []
        assert server.leased_count == 0
        assert not events.parent.exists()
        release.set()
        await raw.aclose()
        tg.cancel_scope.cancel()
