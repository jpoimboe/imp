import json
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import _git, _seed_control


def _ctx(ch: Change, state: State) -> ToolContext:
    ctx = ToolContext(project_root=ch.project_root,
                      events_path=ch.events_path)
    ctx.change = ch
    ctx.change_state = state
    return ctx


def test_tool_registered() -> None:
    assert "create_and_enter_worktree" in get_registry()


def test_tool_flips_project_root_to_worktree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", quest_id="t1", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    assert ctx.project_root == tmp_path                   # main checkout before go-live
    tool = get_registry()["create_and_enter_worktree"]
    msg = tool.invoke(ctx, {})
    wt = tmp_path / ".imp" / "worktrees" / "t1"
    assert ctx.project_root == wt                         # flipped live, same turn
    assert ch.project_root == wt and ch.materialized is True
    meta = json.loads((ch.change_dir / "change.json").read_text())
    assert meta["project_root"] == str(wt) and meta["materialized"] is True
    assert "head_changed=false" in msg


def test_tool_reports_head_changed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", quest_id="t2", base_branch="main")
    (tmp_path / "f.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "advance")
    tool = get_registry()["create_and_enter_worktree"]
    msg = tool.invoke(_ctx(ch, State.load(ch.state_dir)), {})
    assert "head_changed=true" in msg


def test_tool_idempotent(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", quest_id="t3", base_branch="main")
    tool = get_registry()["create_and_enter_worktree"]
    ctx = _ctx(ch, State.load(ch.state_dir))
    tool.invoke(ctx, {})
    msg2 = tool.invoke(ctx, {})
    assert "head_changed=false" in msg2
    assert ctx.project_root == ch.project_root


def test_tool_from_subagent_raises(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Change.create_draft(tmp_path, title="X", quest_id="t4", base_branch="main")
    ctx = ToolContext(project_root=ch.project_root,
                      events_path=ch.events_path)
    ctx.change = ch
    ctx.change_state = None                               # subagent: no go-live authority
    tool = get_registry()["create_and_enter_worktree"]
    with pytest.raises(RuntimeError):
        tool.invoke(ctx, {})
