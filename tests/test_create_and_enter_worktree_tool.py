import json
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.quest import Quest
from imp.engine.state import State
from tests.conftest import _git, _seed_control


def _ctx(ch: Quest, state: State) -> ToolContext:
    ctx = ToolContext(project_root=ch.project_root,
                      events_path=ch.events_path)
    ctx.quest = ch
    ctx.quest_state = state
    return ctx


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_tool_registered() -> None:
    assert "create_and_enter_worktree" in get_registry()


@pytest.mark.anyio
async def test_tool_flips_project_root_to_worktree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="t1", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    assert ctx.project_root == tmp_path                   # main checkout before go-live
    tool = get_registry()["create_and_enter_worktree"]
    msg = await tool.async_invoke(ctx, {})
    wt = tmp_path / ".imp" / "worktrees" / "t1"
    assert ctx.project_root == wt                          # flipped live, same turn
    assert ch.project_root == wt and ch.materialized is True
    meta = json.loads((ch.quest_dir / "quest.json").read_text())
    assert meta["project_root"] == str(wt) and meta["materialized"] is True
    assert "head_changed=false" in msg


@pytest.mark.anyio
async def test_tool_reports_head_changed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="t2", base_branch="main")
    (tmp_path / "f.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "advance")
    tool = get_registry()["create_and_enter_worktree"]
    msg = await tool.async_invoke(_ctx(ch, State.load(ch.state_dir)), {})
    assert "head_changed=true" in msg


@pytest.mark.anyio
async def test_tool_idempotent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="t3", base_branch="main")
    tool = get_registry()["create_and_enter_worktree"]
    ctx = _ctx(ch, State.load(ch.state_dir))
    await tool.async_invoke(ctx, {})
    msg2 = await tool.async_invoke(ctx, {})
    assert "head_changed=false" in msg2
    assert ctx.project_root == ch.project_root


@pytest.mark.anyio
async def test_tool_from_subagent_raises(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="t4", base_branch="main")
    ctx = ToolContext(project_root=ch.project_root, events_path=ch.events_path)
    ctx.quest = ch
    ctx.quest_state = None                                  # subagent: no go-live authority
    tool = get_registry()["create_and_enter_worktree"]
    with pytest.raises(RuntimeError):
        await tool.async_invoke(ctx, {})


def test_tool_description_mentions_dependency_wait() -> None:
    desc = get_registry()["create_and_enter_worktree"].description
    assert (
        "If this quest depends on a quest that hasn't merged yet, it first "
        "waits for that merge before forking"
    ) in desc
    assert "the wait is interruptible (Ctrl-D pauses; a later resume retries)" in desc
    assert desc.rstrip().endswith("Idempotent — a no-op once the worktree already exists.")
