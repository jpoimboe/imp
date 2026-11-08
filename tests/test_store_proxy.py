from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

from imp.cli.store import (
    GaugeWeek,
    LocalStore,
    SearchZones,
    _run_control_root,
    make_store,
)
from imp.cli.store_proxy import ProxyStore, run_query
from imp.engine import worktree
from imp.engine.quest import Quest
from imp.engine.worktree import PreconditionError
from imp.paths import display_path
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
    monkeypatch.setenv("IMP_PROJECT_ROOT", str(tmp_path))
    store = make_store(tmp_path)
    assert isinstance(store, ProxyStore)
    assert store._path == "/tmp/imp-x/y.sock"


def test_make_store_returns_local_when_env_unset(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    assert isinstance(make_store(tmp_path), LocalStore)


def test_make_store_refuses_a_control_root_foreign_to_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, foreign = tmp_path / "run", tmp_path / "foreign"
    for root in (run, foreign):
        root.mkdir()
        _seed_control(root)
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")
    monkeypatch.setenv("IMP_PROJECT_ROOT", str(run))
    with pytest.raises(PreconditionError) as exc:
        make_store(foreign)
    assert str(exc.value) == (
        f"{display_path(foreign)} is a different project from this run's "
        f"({display_path(run)})\n"
        "  to use that project's own imp state, clear the run's pins: "
        "IMP_SESSION_SOCKET= IMP_STATE_DIR= imp …")


def test_run_control_root_resolves_once_per_project_root_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        root.mkdir()
        _seed_control(root)
    _run_control_root.cache_clear()
    resolved: list[Path] = []
    real = worktree.find_control_root

    def _counting(start: Path) -> Path:
        resolved.append(start)
        return real(start)

    monkeypatch.setattr(worktree, "find_control_root", _counting)
    monkeypatch.setenv("IMP_SESSION_SOCKET", "/tmp/imp-x/y.sock")
    monkeypatch.setenv("IMP_PROJECT_ROOT", str(a))
    for _ in range(3):
        assert isinstance(make_store(a), ProxyStore)
    assert resolved == [a]
    monkeypatch.setenv("IMP_PROJECT_ROOT", str(b))
    for _ in range(3):
        assert isinstance(make_store(b), ProxyStore)
    assert resolved == [a, b]
    # The cached root follows the env value: a is now foreign.
    with pytest.raises(PreconditionError):
        make_store(a)
    assert resolved == [a, b]


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


_RESET = 1780642800


def _weeks_homes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A neighbour in another project with a 30-point reading and 3.0 units, and
    the live subject `samo71` with its own 10-point reading."""
    monkeypatch.setenv("IMP_HOME", str(tmp_path / "imphome"))
    qdir = tmp_path / "imphome" / "projects" / "other" / "quests" / "fego87"
    qdir.mkdir(parents=True)
    (qdir / "quest.json").write_text(json.dumps({
        "id": "fego87", "title": "fego87", "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00"}))
    (qdir / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in [
        {"id": "am", "agent_id": "step:a", "timestamp": "2026-06-01T04:00:00+00:00",
         "type": "agent_message",
         "payload": {"provider": "claude", "model": "claude-sonnet-4-6",
                     "auth_mode": "subscription"}},
        {"id": "qk", "agent_id": "step:a", "timestamp": "2026-06-01T04:20:00+00:00",
         "type": "quota_reading",
         "payload": {"provider": "claude", "windows": [
             {"label": "7d", "used_pct": 30.0, "resets_at": _RESET}]}},
        {"id": "qr", "agent_id": "step:a", "timestamp": "2026-06-01T04:30:00+00:00",
         "type": "query_result",
         "payload": {"session_id": "s1", "duration_ms": 30000,
                     "model_usage": {"claude-sonnet-4-6": {"outputTokens": 200_000}}}}]))
    _seed_control(tmp_path)
    subject = Quest.create(tmp_path, title="Subject", quest_id="samo71",
                           base_branch="main")
    _append_event(subject, {
        "id": "own", "agent_id": "step:b", "timestamp": "2026-06-01T04:05:00+00:00",
        "type": "quota_reading",
        "payload": {"provider": "claude", "windows": [
            {"label": "7d", "used_pct": 10.0, "resets_at": _RESET}]}})


@pytest.mark.anyio
async def test_proxy_store_quota_weeks_matches_local(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC 8: a sandboxed render sees the same weeks a local one does."""
    _weeks_homes(tmp_path, monkeypatch)
    local = LocalStore(tmp_path)
    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict) -> object:
        return run_query(local, op, args)

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        proxy = ProxyStore(sock)

        got = await anyio.to_thread.run_sync(proxy.quota_weeks, "samo71")
        assert got == local.quota_weeks("samo71")
        assert got == [GaugeWeek("claude", _RESET, 30.0, 3.0)]

        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_a_store_side_failure_surfaces_as_a_plain_runtime_error(
    tmp_path: Path,
) -> None:
    local = LocalStore(tmp_path)
    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict) -> object:
        if op == "quota_weeks":
            raise OSError("archive unreadable")
        return run_query(local, op, args)

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        proxy = ProxyStore(sock)

        with pytest.raises(RuntimeError) as err:
            await anyio.to_thread.run_sync(proxy.quota_weeks, "samo71")
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
