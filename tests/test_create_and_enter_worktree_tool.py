import json
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.quest import Quest
from imp.engine.sandbox import (
    HomeLayer,
    SandboxConfig,
    SandboxPolicy,
    build_sandbox_argv,
    prune_missing_masks,
    resolve_policy,
)
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
    assert "an attended wait is interruptible (Ctrl-D pauses; a later resume retries)" in desc
    assert "a background run waits on its own until the dependency lands" in desc
    assert desc.rstrip().endswith("Idempotent — a no-op once the worktree already exists.")


def _active_policy(worktree: str, **over: object) -> SandboxPolicy:
    base = dict(
        active=True, worktree=worktree, git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), setenv=(), network=True, note="",
    )
    base.update(over)
    return SandboxPolicy(**base)


@pytest.mark.anyio
async def test_tool_refreshes_sandbox_policy_worktree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="t5", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    layer = HomeLayer(home="/home/u")
    ctx.base_sandbox_policy = _active_policy(
        str(tmp_path), home_layer=layer, readonly_overlays=("/ro/sub",))
    tool = get_registry()["create_and_enter_worktree"]
    await tool.async_invoke(ctx, {})
    wt = tmp_path / ".imp" / "worktrees" / "t5"
    assert ctx.base_sandbox_policy.worktree == str(wt)          # repointed at the fork
    assert ctx.base_sandbox_policy.home_layer is layer          # carried over unchanged
    assert ctx.base_sandbox_policy.readonly_overlays == ("/ro/sub",)


@pytest.mark.anyio
async def test_tool_policy_refresh_noop_when_unsandboxed(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="t6", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    assert ctx.base_sandbox_policy is None
    tool = get_registry()["create_and_enter_worktree"]
    await tool.async_invoke(ctx, {})                            # must not raise
    assert ctx.base_sandbox_policy is None


@pytest.mark.anyio
async def test_read_file_succeeds_in_new_worktree_after_go_live(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="t7", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    ctx.base_sandbox_policy = resolve_policy(
        worktree=tmp_path, control_root=tmp_path, quest_id="t7",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=ch.quest_dir)
    stale = ctx.base_sandbox_policy
    reg = get_registry()
    await reg["create_and_enter_worktree"].async_invoke(ctx, {})
    wt = tmp_path / ".imp" / "worktrees" / "t7"
    (wt / "app.py").write_text("hello from worktree\n")
    # Pre-fix the stale control-root policy would have denied this read.
    assert stale.allows_read(wt / "app.py") is False
    out = await reg["read_file"].async_invoke(ctx, {"path": "app.py"})
    assert "hello from worktree" in out


@pytest.mark.anyio
async def test_bash_argv_chdirs_and_reexposes_new_worktree(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="t8", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    ctx.base_sandbox_policy = resolve_policy(
        worktree=tmp_path, control_root=tmp_path, quest_id="t8",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=ch.quest_dir)
    await get_registry()["create_and_enter_worktree"].async_invoke(ctx, {})
    wt = tmp_path / ".imp" / "worktrees" / "t8"
    argv = build_sandbox_argv(
        "cat app.py", cwd=str(ctx.project_root),
        policy=prune_missing_masks(ctx.sandbox_policy), engine_env={})
    # The shell cwd (ctx.project_root) is the new worktree...
    ci = argv.index("--chdir")
    assert argv[ci + 1] == str(wt)
    # ...and the nested worktree is re-exposed AFTER the .imp tmpfs mask, so files
    # under it are readable. A stale control-root policy emits no such re-bind.
    control_imp = str(tmp_path / ".imp")
    ti = argv.index(control_imp)                    # the --tmpfs <.imp> operand
    binds = [i for i in range(len(argv) - 2)
             if argv[i] == "--bind" and argv[i + 1] == str(wt) and argv[i + 2] == str(wt)]
    assert binds and binds[0] > ti


@pytest.mark.anyio
async def test_tool_recomputes_git_worktree_self_after_go_live(tmp_path: Path) -> None:
    # Go-live must recompute git_worktree_self, else the new worktree's own admin dir stays masked.
    _seed_control(tmp_path)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="t9", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    ctx.base_sandbox_policy = resolve_policy(
        worktree=tmp_path, control_root=tmp_path, quest_id="t9",
        cfg=SandboxConfig(), engine_env_result={}, environ={},
        available=True, quest_dir=ch.quest_dir)
    # Draft policy: mask present, no self re-expose (worktree == control root).
    assert ctx.base_sandbox_policy.git_worktrees_mask == str(tmp_path / ".git" / "worktrees")
    assert ctx.base_sandbox_policy.git_worktree_self == ""
    await get_registry()["create_and_enter_worktree"].async_invoke(ctx, {})
    wt = tmp_path / ".imp" / "worktrees" / "t9"
    pol = ctx.base_sandbox_policy
    assert pol.worktree == str(wt)
    assert pol.git_worktree_self == str(tmp_path / ".git" / "worktrees" / "t9")
    assert pol.git_worktrees_mask == str(tmp_path / ".git" / "worktrees")
    # The new worktree's own admin dir is writable again...
    assert pol.allows_write(tmp_path / ".git" / "worktrees" / "t9" / "HEAD") is True
    # ...while a sibling admin dir stays masked.
    assert pol.allows_write(tmp_path / ".git" / "worktrees" / "other" / "HEAD") is False
