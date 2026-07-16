from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from imp.cli.store import LocalStore, SearchZones, make_store
from imp.cli.store_proxy import ProxyStore, run_query
from imp.engine.quest import Quest
from imp.remote.session.socket_server import SessionSocket
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _append_event(quest: Quest, event: dict) -> None:
    with quest.events_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")


def test_make_store_returns_proxy_when_env_set(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")
    store = make_store(tmp_path)
    assert isinstance(store, ProxyStore)
    assert store._path == "/tmp/imp-x/y.sock"


def test_make_store_returns_local_when_env_unset(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert isinstance(make_store(tmp_path), LocalStore)


@pytest.mark.anyio
async def test_proxy_store_matches_local_store_over_the_socket(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Live", quest_id="liveq", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "opening_intent", "payload": {"text": "wire the widget"}})
    _append_event(ch, {
        "id": "e2", "timestamp": "2026-06-01T00:00:05+00:00",
        "type": "user_message",
        "payload": {"human": True,
                    "content": [{"type": "text", "text": "darkmode toggle"}]}})

    local = LocalStore(tmp_path)
    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict) -> object:
        return run_query(local, op, args)

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        proxy = ProxyStore(sock)

        # get_events (list[dict]) equal across backends
        assert await anyio.to_thread.run_sync(proxy.get_events, "liveq") == \
            local.get_events("liveq")
        assert await anyio.to_thread.run_sync(proxy.get_events, "nosuch") is None

        # scan_homes (bytes-bearing HomeDigest) equal across backends
        p_homes = await anyio.to_thread.run_sync(proxy.scan_homes)
        assert p_homes == local.scan_homes()
        assert {h.quest_id for h in p_homes} == {"liveq"}

        # search round-trips the SearchZones arg
        zones = SearchZones(active=True, backlog=False, statuses=frozenset(),
                            merged_after=None)
        p_hits = await anyio.to_thread.run_sync(proxy.search, "darkmode", zones)
        assert p_hits == local.search("darkmode", zones)
        assert [h.quest_id for h in p_hits] == ["liveq"]

        # quest_card (None-able + summary bytes) equal across backends
        assert await anyio.to_thread.run_sync(proxy.quest_card, "liveq") == \
            local.quest_card("liveq")
        assert await anyio.to_thread.run_sync(proxy.quest_card, "nosuch") is None

        await session.close()
        tg.cancel_scope.cancel()
