from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

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
from imp.paths import display_path, project_state_dir
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


_OK = "artq01"
_LEGACY, _LEGACY_REF = "am2asr", "1p5txc"
_DATED, _DATED_REF = "2026-06-17-u43m6g", "2026-06-18-r9c1zz"
_LINKED_ROOT = "rootln"


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _home(control: Path, quest_id: str) -> Path:
    return project_state_dir(control) / "quests" / quest_id


async def _ask(fn: Callable[..., Any], *args: Any) -> Any:
    return await anyio.to_thread.run_sync(fn, *args)


def _membership_fixture(control: Path, outside: Path) -> None:
    """Live regular files beside links out, a two-commit ref, a symlinked root,
    and bait that a path-built id would reach without the id check."""
    _seed_control(control)
    _write(outside, "secret.md", "SECRET\n")
    _write(outside, "dir/inner.md", "INNER\n")
    _write(outside, f"spec/{_LINKED_ROOT}.md", "SECRET SPEC\n")

    Quest.create(control, title="Art", quest_id=_OK, base_branch="main")
    art = _home(control, _OK) / "artifacts"
    _write(art, f"spec/{_OK}.md", "# live spec\n")
    _write(art, f"plans/{_OK}-plan-1.md", "# plan one\n")
    (art / "leak.md").symlink_to(outside / "secret.md")
    (art / "linkdir").symlink_to(outside / "dir")
    # Two commits, so `<id>~1` names a real commit.
    worktree.artifact_commit(control, _OK, {f"spec/{_OK}.md": b"# ref v1\n"},
                             message="artifact: 1")
    worktree.artifact_commit(control, _OK, {f"spec/{_OK}.md": b"# ref v2\n"},
                             message="artifact: 2")
    _write(_home(control, _OK) / "sub" / "artifacts", "bait.md", "BAIT\n")

    Quest.create(control, title="Linked", quest_id=_LINKED_ROOT, base_branch="main")
    worktree.artifact_commit(
        control, _LINKED_ROOT, {f"spec/{_LINKED_ROOT}.md": b"# ref spec\n"},
        message="artifact: seed")
    (_home(control, _LINKED_ROOT) / "artifacts").symlink_to(outside)


@pytest.mark.anyio
async def test_proxy_saved_files_list_and_read_only_listed_files(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory,
) -> None:
    _membership_fixture(tmp_path, tmp_path_factory.mktemp("outside"))
    local = LocalStore(tmp_path)
    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict) -> object:
        return run_query(local, op, args)

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        proxy = ProxyStore(sock)

        # The live listing round-trips without the links; a listed file reads.
        assert await _ask(proxy.saved_files, _OK) == [
            f"plans/{_OK}-plan-1.md", f"spec/{_OK}.md"]
        assert await _ask(proxy.read_saved_file, _OK, f"spec/{_OK}.md") == \
            b"# live spec\n"
        card = await _ask(proxy.quest_card, _OK)
        assert card is not None and card.files == [
            f"plans/{_OK}-plan-1.md", f"spec/{_OK}.md"]
        # Links, traversal and absolute paths read nothing.
        for rel in ("leak.md", "linkdir/inner.md", "../quest.json",
                    f"../../quests/{_OK}/quest.json", "/etc/passwd",
                    f"spec/../spec/{_OK}.md"):
            assert await _ask(proxy.read_saved_file, _OK, rel) is None, rel
        # Path and git rev syntax in the id selects no home and no ref.
        for bad in (f"{_OK}~1", f"{_OK}^{{tree}}", f"../quests/{_OK}",
                    f"{_OK}/sub", "..", "", f"{_OK}\n"):
            assert await _ask(proxy.saved_files, bad) == [], bad
            assert await _ask(proxy.read_saved_file, bad, f"spec/{_OK}.md") is None
            assert await _ask(proxy.read_saved_file, bad, "bait.md") is None
            assert await _ask(proxy.quest_card, bad) is None, bad
        # A symlinked artifacts/ counts as absent: the ref answers, the target never.
        assert await _ask(proxy.saved_files, _LINKED_ROOT) == [
            f"spec/{_LINKED_ROOT}.md"]
        assert await _ask(
            proxy.read_saved_file, _LINKED_ROOT, f"spec/{_LINKED_ROOT}.md") == \
            b"# ref spec\n"
        assert await _ask(proxy.read_saved_file, _LINKED_ROOT, "secret.md") is None
        card = await _ask(proxy.quest_card, _LINKED_ROOT)
        assert card is not None and card.files == [f"spec/{_LINKED_ROOT}.md"]

        await session.close()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_proxy_saved_files_serve_every_archived_id_shape(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    for qid in (_LEGACY, _DATED):
        Quest.create(tmp_path, title=f"Q {qid}", quest_id=qid, base_branch="main")
        _write(_home(tmp_path, qid) / "artifacts", f"summary/{qid}.md",
               f"summary of {qid}\n")
    for qid in (_LEGACY_REF, _DATED_REF):
        Quest.create(tmp_path, title=f"Q {qid}", quest_id=qid, base_branch="main")
        worktree.artifact_commit(
            tmp_path, qid, {f"summary/{qid}.md": f"summary of {qid}\n".encode()},
            message="artifact: seed")
    worktree.artifact_commit(
        tmp_path, _LEGACY_REF,
        {"notes/café.md": "café notes\n".encode(),
         f"summary/{_LEGACY_REF}.md": f"summary of {_LEGACY_REF}\n".encode()},
        message="artifact: notes")
    local = LocalStore(tmp_path)
    sock = str(tmp_path / "s.sock")

    async def on_query(op: str, args: dict) -> object:
        return run_query(local, op, args)

    session = SessionSocket(sock, on_query=on_query)
    async with anyio.create_task_group() as tg:
        await session.open()
        tg.start_soon(session.serve)
        proxy = ProxyStore(sock)

        for qid in (_LEGACY, _DATED, _LEGACY_REF, _DATED_REF):
            rel = f"summary/{qid}.md"
            body = f"summary of {qid}\n".encode()
            assert rel in await _ask(proxy.saved_files, qid), qid
            assert await _ask(proxy.read_saved_file, qid, rel) == body
            card = await _ask(proxy.quest_card, qid)
            assert card is not None and card.summary == body, qid
        # A non-ASCII name lists and reads from the ref only.
        assert await _ask(proxy.saved_files, _LEGACY_REF) == [
            "notes/café.md", f"summary/{_LEGACY_REF}.md"]
        assert await _ask(proxy.read_saved_file, _LEGACY_REF, "notes/café.md") == \
            "café notes\n".encode()

        await session.close()
        tg.cancel_scope.cancel()
