from __future__ import annotations

import contextlib
import functools
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.cli import store_proxy
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from imp.remote.daemon.directory import session_is_resident
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import serve_quest_session
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import QUERY_USER_RENAME
from tests.async_wait import HANG_BACKSTOP, wait_until
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seeded_quest(control: Path, quest_id: str, *, materialized: bool) -> Quest:
    _seed_control(control)
    Quest.create_draft(control, title="Original", quest_id=quest_id, base_branch="main")
    if materialized:
        meta_path = project_state_dir(control) / "quests" / quest_id / "quest.json"
        meta = json.loads(meta_path.read_text())
        meta["materialized"] = True
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True))
    return Quest.load(control, quest_id)


def _renames(quest: Quest) -> list[dict[str, Any]]:
    return [e for e in quest.event_log().read()
            if e["type"] in ("quest_renamed", "user_renamed")]


async def _parked_run(
    quest: Quest, *, on_event: Any, user_chat_input_fn: Any,
    coordinator: Any, wait_for_turn_fn: Any = None,
) -> None:
    await anyio.sleep_forever()


@contextlib.asynccontextmanager
async def _resident(quest: Quest) -> AsyncIterator[str]:
    """Serve `quest` from a real session daemon whose run stays parked."""
    async with anyio.create_task_group() as tg:
        tg.start_soon(functools.partial(
            serve_quest_session, quest, run_fn=_parked_run, hibernate=False))
        await wait_until(lambda: session_is_resident(quest.control_root, quest.id))
        yield str(session_socket_path(quest.id))
        tg.cancel_scope.cancel()


@contextlib.asynccontextmanager
async def _attached(sock: str, quest: Quest) -> AsyncIterator[list[dict[str, Any]]]:
    """An attached client whose replay is complete: later events arrive live."""
    seen: list[dict[str, Any]] = []
    client = SessionClient(sock, quest.id, quest.title, on_event=seen.append)
    await client.connect()
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(client.receive_events)
            await wait_until(lambda: any(e.get("type") == "replay_complete" for e in seen))
            yield seen
            tg.cancel_scope.cancel()
    finally:
        await client.aclose()


def _live_user_renames(seen: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [e for e in seen if e.get("type") == "user_renamed" and not e.get("_replay")]


@pytest.mark.anyio
async def test_daemon_user_rename_op_broadcasts_and_updates_memory(
        tmp_path: Path) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren01", materialized=True)
    async with _resident(quest) as sock, _attached(sock, quest) as seen:
        with anyio.fail_after(HANG_BACKSTOP):
            assert await store_proxy.roundtrip(
                sock, QUERY_USER_RENAME, {"title": "X"}) is None
        assert quest.title == "X"
        await wait_until(lambda: bool(_live_user_renames(seen)))
    live = _live_user_renames(seen)
    assert len(live) == 1
    assert live[0]["payload"] == {
        "id": quest.id, "old_title": "Original", "new_title": "X"}
    assert [e["type"] for e in _renames(quest)] == ["user_renamed"]
    assert Quest.load(tmp_path, quest.id).title == "X"


@pytest.mark.anyio
async def test_daemon_user_rename_op_on_draft_sets_title_without_event(
        tmp_path: Path) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren02", materialized=False)
    async with _resident(quest) as sock:
        with anyio.fail_after(HANG_BACKSTOP):
            await store_proxy.roundtrip(sock, QUERY_USER_RENAME, {"title": "X"})
        assert quest.title == "X"
    assert Quest.load(tmp_path, quest.id).title == "X"
    assert _renames(quest) == []
