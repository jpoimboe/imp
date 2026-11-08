from __future__ import annotations

import argparse
import contextlib
import functools
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.cli import main, store_proxy
from imp.engine.quest import Quest
from imp.paths import project_state_dir
from imp.remote.daemon.directory import session_is_resident
from imp.remote.session.client import SessionClient
from imp.remote.session.daemon import serve_quest_session
from imp.remote.session.discover import session_socket_path
from imp.remote.session.protocol import QUERY_USER_RENAME, encode_result_error
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


def _edit_args(token: str, title: str) -> argparse.Namespace:
    return argparse.Namespace(
        token=token, title=title, backlog=False, active=False,
        depends_on=None, clear_deps=False, append_intent=None, base=None)


async def _edit(quest_id: str, title: str) -> int:
    """`imp edit <id> --title <title>` off-loop, so the loop stays free to serve the daemon."""
    with anyio.fail_after(HANG_BACKSTOP):
        return await anyio.to_thread.run_sync(
            main._cmd_edit, _edit_args(quest_id, title), abandon_on_cancel=True)


@contextlib.asynccontextmanager
async def _fake_daemon(
    quest_id: str, reply: Callable[[SocketStream], Awaitable[None]],
) -> AsyncIterator[list[str]]:
    """A listener on quest_id's session socket that records each query op, then `reply`s."""
    path = session_socket_path(quest_id)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    ops: list[str] = []

    async def _handle(stream: SocketStream) -> None:
        async with stream:
            try:
                line = await BufferedByteReceiveStream(stream).receive_until(b"\n", 1 << 20)
            except (anyio.EndOfStream, anyio.IncompleteRead):
                return  # a liveness probe
            ops.append(json.loads(line)["op"])
            await reply(stream)

    listener = await anyio.create_unix_listener(path)
    async with listener, anyio.create_task_group() as tg:
        tg.start_soon(listener.serve, _handle)
        yield ops
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_edit_title_routes_to_resident_daemon(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren03", materialized=True)
    monkeypatch.chdir(tmp_path)
    async with _resident(quest) as sock, _attached(sock, quest) as seen:
        assert await _edit(quest.id, "X") == 0
        assert quest.title == "X"
        await wait_until(lambda: bool(_live_user_renames(seen)))
    assert len(_live_user_renames(seen)) == 1
    assert [e["type"] for e in _renames(quest)] == ["user_renamed"]
    assert Quest.load(tmp_path, quest.id).title == "X"


@pytest.mark.anyio
async def test_edit_title_on_resident_draft_renames_in_daemon(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren04", materialized=False)
    monkeypatch.chdir(tmp_path)
    async with _resident(quest):
        assert await _edit(quest.id, "X") == 0
        assert quest.title == "X"
    assert Quest.load(tmp_path, quest.id).title == "X"
    assert _renames(quest) == []


@pytest.mark.anyio
async def test_proxied_edit_title_served_by_own_daemon_does_not_deadlock(
        tmp_path: Path) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren05", materialized=True)
    async with _resident(quest) as sock:
        with anyio.fail_after(HANG_BACKSTOP):
            result = await store_proxy.roundtrip(sock, "edit", {
                "token": quest.id, "title": "X", "backlog": False, "active": False,
                "depends_on": None, "clear_deps": False, "append_intent": None,
                "base": None})
        assert result["returncode"] == 0
        # A self-delivery deadlock would time out into the local write and leave this stale.
        assert quest.title == "X"
    assert [e["type"] for e in _renames(quest)] == ["user_renamed"]


@pytest.mark.parametrize("exc", [OSError("daemon gone"), TimeoutError()])
def test_edit_title_falls_back_when_delivery_raises(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren06", materialized=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "imp.remote.daemon.directory.session_is_resident", lambda _c, _q: True)
    attempts: list[tuple[str, str]] = []

    def _fail(quest_id: str, title: str) -> None:
        attempts.append((quest_id, title))
        raise exc

    monkeypatch.setattr(main, "_deliver_rename_resident", _fail)
    assert main._cmd_edit(_edit_args(quest.id, "X")) == 0
    assert attempts == [(quest.id, "X")]
    assert Quest.load(tmp_path, quest.id).title == "X"
    assert [e["type"] for e in _renames(quest)] == ["user_renamed"]


def test_edit_title_fallback_after_daemon_applied_logs_one_rename(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren07", materialized=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "imp.remote.daemon.directory.session_is_resident", lambda _c, _q: True)

    def _applied_then_timed_out(quest_id: str, title: str) -> None:
        daemon_side = Quest.load(tmp_path, quest_id)
        daemon_side.rename(title, by="user", log=daemon_side.event_log())
        raise TimeoutError

    monkeypatch.setattr(main, "_deliver_rename_resident", _applied_then_timed_out)
    assert main._cmd_edit(_edit_args(quest.id, "X")) == 0
    assert Quest.load(tmp_path, quest.id).title == "X"
    assert [e["type"] for e in _renames(quest)] == ["user_renamed"]


async def _reply_unknown_op(stream: SocketStream) -> None:
    await stream.send(
        (encode_result_error("unknown store op 'user_rename'") + "\n").encode("utf-8"))


async def _never_reply(stream: SocketStream) -> None:
    await anyio.sleep_forever()


async def _hang_up(stream: SocketStream) -> None:
    return None


@pytest.mark.anyio
async def test_edit_title_falls_back_on_error_reply(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren08", materialized=True)
    monkeypatch.chdir(tmp_path)
    async with _fake_daemon(quest.id, _reply_unknown_op) as ops:
        assert await _edit(quest.id, "X") == 0
        assert ops == [QUERY_USER_RENAME]
    assert Quest.load(tmp_path, quest.id).title == "X"
    assert [e["type"] for e in _renames(quest)] == ["user_renamed"]


@pytest.mark.anyio
async def test_edit_title_falls_back_when_daemon_never_replies(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren09", materialized=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(main, "_RESIDENT_RENAME_TIMEOUT", 0.5)
    async with _fake_daemon(quest.id, _never_reply):
        assert await _edit(quest.id, "X") == 0
    assert Quest.load(tmp_path, quest.id).title == "X"
    assert [e["type"] for e in _renames(quest)] == ["user_renamed"]


@pytest.mark.anyio
async def test_edit_title_falls_back_when_daemon_hangs_up(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quest = _seeded_quest(tmp_path, "2026-11-07-ren10", materialized=True)
    monkeypatch.chdir(tmp_path)
    async with _fake_daemon(quest.id, _hang_up) as ops:
        assert await _edit(quest.id, "X") == 0
        assert ops == [QUERY_USER_RENAME]
    assert Quest.load(tmp_path, quest.id).title == "X"
    assert [e["type"] for e in _renames(quest)] == ["user_renamed"]
