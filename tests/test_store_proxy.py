from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from imp.cli.store import (
    LocalStore,
    QuotaNeighboursUnsupported,
    SearchZones,
    make_store,
)
from imp.cli.store_proxy import ProxyStore, run_query
from imp.engine.quest import Quest
from imp.remote.session.socket_server import SessionSocket
from tests.conftest import _archive_with_event, _seed_control


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
async def test_proxy_store_search_event_candidates_matches_local(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    line = json.dumps({
        "id": "e0", "type": "tool_call",
        "payload": {"name": "bash", "input": {"cmd": "GREPTARGET"}}}) + "\n"
    _archive_with_event(tmp_path, "arch01", "Archived one", line)

    local = LocalStore(tmp_path)
    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict) -> object:
        return run_query(local, op, args)

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        proxy = ProxyStore(sock)

        p_cands = await anyio.to_thread.run_sync(proxy.search_event_candidates, "GREPTARGET")
        assert p_cands == local.search_event_candidates("GREPTARGET")
        assert p_cands == [("arch01", "Archived one", "abandoned", None)]
        assert all(isinstance(c, tuple) for c in p_cands)

        await session.close()
        tg.cancel_scope.cancel()


_QUOTA_START = "2026-06-01T03:30:00+00:00"
_QUOTA_END = "2026-06-01T05:38:00+00:00"


def _neighbour_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quest_id: str,
) -> None:
    """A live quest home in another project with one in-span query_result."""
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    qdir = tmp_path / "imphome" / "projects" / "other" / "quests" / quest_id
    qdir.mkdir(parents=True)
    (qdir / "quest.json").write_text(json.dumps({
        "id": quest_id, "title": quest_id, "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00"}))
    (qdir / "events.jsonl").write_text(json.dumps({
        "id": "qr", "agent_id": "step:a",
        "timestamp": "2026-06-01T04:30:00+00:00", "type": "query_result",
        "payload": {"session_id": "s1", "duration_ms": 30000, "usage": {},
                    "model_usage": {}}}) + "\n")


@pytest.mark.anyio
async def test_proxy_store_quota_neighbours_matches_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _neighbour_home(tmp_path, monkeypatch, "fego87")
    local = LocalStore(tmp_path)
    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict) -> object:
        return run_query(local, op, args)

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        proxy = ProxyStore(sock)

        got = await anyio.to_thread.run_sync(
            proxy.quota_neighbours, "samo71", _QUOTA_START, _QUOTA_END)
        assert got == local.quota_neighbours("samo71", _QUOTA_START, _QUOTA_END)
        assert [(n.project, n.quest_id) for n in got] == [("other", "fego87")]
        assert [u["id"] for u in got[0].usage] == ["qr"]

        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_daemon_that_does_not_know_the_op_is_its_own_condition(
    tmp_path: Path,
) -> None:
    local = LocalStore(tmp_path)
    sock = str(tmp_path / "s.sock")
    failure = {"kind": "unknown"}

    async def on_query(op: str, args: dict) -> object:
        if op == "quota_neighbours":
            if failure["kind"] == "unknown":
                raise ValueError(f"unknown store op {op!r}")   # pre-upgrade daemon
            raise OSError("archive unreadable")
        return run_query(local, op, args)

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        proxy = ProxyStore(sock)

        with pytest.raises(QuotaNeighboursUnsupported):
            await anyio.to_thread.run_sync(
                proxy.quota_neighbours, "samo71", _QUOTA_START, _QUOTA_END)

        failure["kind"] = "broken"
        with pytest.raises(RuntimeError) as err:
            await anyio.to_thread.run_sync(
                proxy.quota_neighbours, "samo71", _QUOTA_START, _QUOTA_END)
        assert not isinstance(err.value, QuotaNeighboursUnsupported)
        assert "archive unreadable" in str(err.value)

        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_proxy_store_matches_local_store_over_the_socket(
    tmp_path: Path,
) -> None:
    _seed_control(tmp_path)
    ch = Quest.create(tmp_path, title="Live", quest_id="liveq", base_branch="main")
    _append_event(ch, {
        "id": "e1", "timestamp": "2026-06-01T00:00:00+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "intent",
                    "content": [{"type": "text", "text": "wire the widget"}]}})
    _append_event(ch, {
        "id": "e2", "timestamp": "2026-06-01T00:00:05+00:00",
        "type": "user_message",
        "payload": {"role": "user", "subtype": "reply",
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
