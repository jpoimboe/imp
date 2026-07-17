from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _seed_control


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _FakeCoord:
    def __init__(self) -> None:
        self.calls: list[str | None] = []

    def set_save_hook(self, fn: Any) -> None:
        pass

    def request_save(self, reason: str | None = None) -> None:
        self.calls.append(reason)


def _ctx(quest: Quest, *, coordinator: Any) -> ToolContext:
    ctx = ToolContext(project_root=quest.project_root, events_path=quest.events_path)
    ctx.quest = quest
    ctx.quest_state = State.load(quest.state_dir)
    ctx.coordinator = coordinator
    return ctx


def _draft(tmp_path: Path, qid: str) -> Quest:
    return Quest.create_draft(tmp_path, title="X", quest_id=qid, base_branch="main")


def test_save_and_exit_registered() -> None:
    reg = get_registry()
    assert "save_and_exit" in reg
    tool = reg["save_and_exit"]
    assert tool.description == (
        "Cleanly save this conversation and drop to the shell: a graceful pause "
        "at the current step — nothing built, no worktree — resumable later with "
        "`imp resume <id>`. Use when the user chooses to set the quest aside "
        "instead of building now."
    )
    assert tool.input_schema.get("properties", {}) == {}


@pytest.mark.anyio
async def test_requests_clean_save_and_does_not_materialize(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "s1")
    coord = _FakeCoord()
    tool = get_registry()["save_and_exit"]
    # The tool parks forever; move_on_after unwinds it after the request lands.
    with anyio.move_on_after(0.1) as cancel_scope:
        await tool.async_invoke(_ctx(q, coordinator=coord), {})
    assert cancel_scope.cancelled_caught
    assert coord.calls == [None]
    assert q.materialized is False


@pytest.mark.anyio
async def test_requires_top_level_step(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "s2")
    ctx = _ctx(q, coordinator=_FakeCoord())
    ctx.quest_state = None
    tool = get_registry()["save_and_exit"]
    with pytest.raises(RuntimeError):
        await tool.async_invoke(ctx, {})


@pytest.mark.anyio
async def test_requires_coordinator(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    q = _draft(tmp_path, "s3")
    ctx = _ctx(q, coordinator=None)
    tool = get_registry()["save_and_exit"]
    with pytest.raises(RuntimeError):
        await tool.async_invoke(ctx, {})
