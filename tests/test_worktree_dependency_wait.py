from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import imp.agents.tools.create_and_enter_worktree as cew
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine import backlog
from imp.engine.awaiting_user_input import AwaitingUserInput
from imp.engine.quest import Quest
from imp.engine.state import State
from imp.paths import project_state_dir
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _RecordingLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, *, agent_id: Any = None, type: str, payload: dict) -> dict:
        self.events.append((type, payload))
        return {}


class _Coord:
    def __init__(self, should_pause: bool = False) -> None:
        self.should_pause = should_pause


def _ctx(quest: Quest, *, coordinator: Any = None, event_log: Any = None,
         non_interactive: bool = False) -> ToolContext:
    ctx = ToolContext(project_root=quest.project_root, events_path=quest.events_path)
    ctx.quest = quest
    ctx.quest_state = State.load(quest.state_dir)
    ctx.coordinator = coordinator
    ctx.event_log = event_log
    ctx.non_interactive = non_interactive
    return ctx


def _draft(tmp_path: Path, qid: str, deps: list[str]) -> Quest:
    return Quest.create_draft(tmp_path, title="X", quest_id=qid,
                              base_branch="main", depends_on=deps)


@pytest.mark.anyio
async def test_waits_for_unmerged_dep_then_materializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep1", ["a"])
    calls = {"n": 0}

    def _readiness(cr: Path, deps: list[str]) -> tuple[bool, list[str]]:
        calls["n"] += 1
        return (True, []) if calls["n"] >= 3 else (False, ["a"])

    monkeypatch.setattr(backlog, "readiness", _readiness)
    slept = {"n": 0}

    async def _sleep(delay: float, coord: Any) -> None:
        slept["n"] += 1

    monkeypatch.setattr(cew, "interruptible_sleep", _sleep)
    log = _RecordingLog()
    ctx = _ctx(q, coordinator=_Coord(), event_log=log)
    tool = get_registry()["create_and_enter_worktree"]

    await tool.async_invoke(ctx, {})

    wt = project_state_dir(tmp_path) / "worktrees" / "dep1"
    assert ctx.project_root == wt and q.materialized is True
    assert slept["n"] == 1
    assert [t for t, _ in log.events] == ["dependency_waiting", "dependency_ready"]
    assert log.events[0][1]["depends_on"] == ["a"]


@pytest.mark.anyio
async def test_materializes_immediately_when_deps_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep2", ["a"])
    monkeypatch.setattr(backlog, "readiness", lambda cr, deps: (True, []))
    log = _RecordingLog()
    tool = get_registry()["create_and_enter_worktree"]

    await tool.async_invoke(_ctx(q, coordinator=_Coord(), event_log=log), {})

    assert q.materialized is True
    assert log.events == []


@pytest.mark.anyio
async def test_no_declared_deps_skips_the_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep3", [])

    def _boom(cr: Path, deps: list[str]) -> tuple[bool, list[str]]:
        raise AssertionError("readiness must not be consulted with no deps")

    monkeypatch.setattr(backlog, "readiness", _boom)
    log = _RecordingLog()
    tool = get_registry()["create_and_enter_worktree"]

    await tool.async_invoke(_ctx(q, coordinator=_Coord(), event_log=log), {})

    assert q.materialized is True
    assert log.events == []


@pytest.mark.anyio
async def test_ctrl_d_during_wait_pauses_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep4", ["a"])
    monkeypatch.setattr(backlog, "readiness", lambda cr, deps: (False, ["a"]))
    log = _RecordingLog()
    ctx = _ctx(q, coordinator=_Coord(should_pause=True), event_log=log)
    tool = get_registry()["create_and_enter_worktree"]

    with pytest.raises(AwaitingUserInput) as ei:
        await tool.async_invoke(ctx, {})

    assert ei.value.interrupt is True
    assert q.materialized is False
    assert [t for t, _ in log.events] == ["dependency_waiting"]


@pytest.mark.anyio
async def test_headless_waits_for_unmerged_dep_then_materializes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep5", ["a"])
    calls = {"n": 0}

    def _readiness(cr: Path, deps: list[str]) -> tuple[bool, list[str]]:
        calls["n"] += 1
        return (True, []) if calls["n"] >= 3 else (False, ["a"])

    monkeypatch.setattr(backlog, "readiness", _readiness)
    slept = {"n": 0}

    async def _sleep(delay: float, coord: Any) -> None:
        slept["n"] += 1

    monkeypatch.setattr(cew, "interruptible_sleep", _sleep)
    log = _RecordingLog()
    ctx = _ctx(q, coordinator=_Coord(), event_log=log, non_interactive=True)
    tool = get_registry()["create_and_enter_worktree"]

    await tool.async_invoke(ctx, {})

    wt = project_state_dir(tmp_path) / "worktrees" / "dep5"
    assert ctx.project_root == wt and q.materialized is True
    assert slept["n"] == 1
    assert [t for t, _ in log.events] == ["dependency_waiting", "dependency_ready"]
    assert log.events[0][1]["depends_on"] == ["a"]


@pytest.mark.anyio
async def test_waits_for_multiple_deps_partially_merged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep6", ["a", "b"])
    seq = [(False, ["a", "b"]), (False, ["b"]), (True, [])]
    calls = {"n": 0}

    def _readiness(cr: Path, deps: list[str]) -> tuple[bool, list[str]]:
        r = seq[min(calls["n"], len(seq) - 1)]
        calls["n"] += 1
        return r

    monkeypatch.setattr(backlog, "readiness", _readiness)

    async def _sleep(delay: float, coord: Any) -> None:
        pass

    monkeypatch.setattr(cew, "interruptible_sleep", _sleep)
    log = _RecordingLog()
    ctx = _ctx(q, coordinator=_Coord(), event_log=log)
    tool = get_registry()["create_and_enter_worktree"]

    await tool.async_invoke(ctx, {})

    assert q.materialized is True
    assert [t for t, _ in log.events] == ["dependency_waiting", "dependency_ready"]
    assert log.events[0][1]["depends_on"] == ["a", "b"]


@pytest.mark.anyio
async def test_ctrl_d_after_sleep_wakes_pauses_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep7", ["a"])
    monkeypatch.setattr(backlog, "readiness", lambda cr, deps: (False, ["a"]))
    coord = _Coord(should_pause=False)

    async def _sleep(delay: float, c: Any) -> None:
        c.should_pause = True

    monkeypatch.setattr(cew, "interruptible_sleep", _sleep)
    log = _RecordingLog()
    ctx = _ctx(q, coordinator=coord, event_log=log)
    tool = get_registry()["create_and_enter_worktree"]

    with pytest.raises(AwaitingUserInput) as ei:
        await tool.async_invoke(ctx, {})

    assert ei.value.interrupt is True
    assert q.materialized is False
    assert [t for t, _ in log.events] == ["dependency_waiting"]
