from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import imp.agents.tools.create_and_enter_worktree as cew
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine import backlog
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


def _ctx(quest: Quest, *, event_log: Any = None) -> ToolContext:
    ctx = ToolContext(project_root=quest.project_root,
                      events_path=quest.events_path)
    ctx.quest = quest
    ctx.quest_state = State.load(quest.state_dir)
    ctx.event_log = event_log
    return ctx


def _draft(tmp_path: Path, qid: str, deps: list[str]) -> Quest:
    return Quest.create_draft(tmp_path, title="X", quest_id=qid,
                              base_branch="main", depends_on=deps)


@pytest.mark.anyio
async def test_unmerged_dep_flags_the_wait_without_materializing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep1", ["a"])
    polls = {"n": 0}

    def _readiness(cr: Path, deps: list[str]) -> tuple[bool, list[str]]:
        polls["n"] += 1
        return (False, ["a"])

    monkeypatch.setattr(backlog, "readiness", _readiness)
    log = _RecordingLog()
    ctx = _ctx(q, event_log=log)
    tool = get_registry()["create_and_enter_worktree"]

    body = await tool.async_invoke(ctx, {})

    wt = project_state_dir(tmp_path) / "worktrees" / "dep1"
    assert ctx.dependency_wait == ("a",)
    assert q.materialized is False and not wt.exists()
    assert ctx.project_root != wt
    assert polls["n"] == 1          # checked exactly once — no poll loop
    assert log.events == []         # the emits moved to the engine
    assert "waiting for a to merge" in body
    assert "take no further action" in body


@pytest.mark.anyio
async def test_multiple_unmerged_deps_are_all_named(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep2", ["a", "b"])
    monkeypatch.setattr(backlog, "readiness",
                        lambda cr, deps: (False, ["a", "b"]))
    ctx = _ctx(q)
    tool = get_registry()["create_and_enter_worktree"]

    body = await tool.async_invoke(ctx, {})

    assert ctx.dependency_wait == ("a", "b")
    assert q.materialized is False
    assert "waiting for a, b to merge" in body


@pytest.mark.anyio
async def test_merged_deps_materialize_with_no_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep3", ["a"])
    monkeypatch.setattr(backlog, "readiness", lambda cr, deps: (True, []))
    log = _RecordingLog()
    ctx = _ctx(q, event_log=log)
    tool = get_registry()["create_and_enter_worktree"]

    body = await tool.async_invoke(ctx, {})

    wt = project_state_dir(tmp_path) / "worktrees" / "dep3"
    assert q.materialized is True and ctx.project_root == wt
    assert ctx.dependency_wait == ()
    assert log.events == []
    assert body.startswith("materialized worktree at")


@pytest.mark.anyio
async def test_no_declared_deps_skips_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "dep4", [])

    def _boom(cr: Path, deps: list[str]) -> tuple[bool, list[str]]:
        raise AssertionError("readiness must not be consulted with no deps")

    monkeypatch.setattr(backlog, "readiness", _boom)
    ctx = _ctx(q)
    tool = get_registry()["create_and_enter_worktree"]

    await tool.async_invoke(ctx, {})

    assert q.materialized is True
    assert ctx.dependency_wait == ()


def test_poll_loop_support_is_gone() -> None:
    for name in ("_DEP_POLL_INTERVAL", "_emit", "_await_dependencies",
                 "interruptible_sleep", "AwaitingUserInput"):
        assert not hasattr(cew, name), f"{name} must be gone from the tool module"
