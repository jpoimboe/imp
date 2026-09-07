import json
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.quest import Quest
from imp.engine.sandbox import (
    HomeLayer,
    OverlayRoot,
    SandboxConfig,
    SandboxPolicy,
    build_sandbox_spawn,
    prepare_spawn_policy,
    resolve_policy,
)
from imp.engine.state import State
from imp.paths import project_state_dir
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
    wt = project_state_dir(tmp_path) / "worktrees" / "t1"
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
    assert desc.rstrip().endswith(
        "Idempotent — once the worktree exists, creating it again is a no-op, "
        "but the project's `worktree_setup` command re-runs on every call.")


def _active_policy(worktree: str, **over: object) -> SandboxPolicy:
    base = dict(
        active=True, worktree=worktree, git_common_dir="/ctl/.git",
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), child_env=(), network=True, note="",
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
        str(tmp_path), home_layer=layer, overlay_root=OverlayRoot(
            path="/q/steps/build/coder.overlay", userxattr=True),
        readonly_overlays=("/ro/sub",))
    tool = get_registry()["create_and_enter_worktree"]
    await tool.async_invoke(ctx, {})
    wt = project_state_dir(tmp_path) / "worktrees" / "t5"
    assert ctx.base_sandbox_policy.worktree == str(wt)          # repointed at the fork
    assert ctx.base_sandbox_policy.home_layer is layer          # carried over unchanged
    assert ctx.base_sandbox_policy.overlay_root == OverlayRoot(
        path="/q/steps/build/coder.overlay", userxattr=True)
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
    wt = project_state_dir(tmp_path) / "worktrees" / "t7"
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
    wt = project_state_dir(tmp_path) / "worktrees" / "t8"
    argv = build_sandbox_spawn(
        "cat app.py", cwd=str(ctx.project_root),
        policy=prepare_spawn_policy(ctx.sandbox_policy), engine_env={},
        environ={}).argv
    # The shell cwd (ctx.project_root) is the new worktree...
    ci = argv.index("--chdir")
    assert argv[ci + 1] == str(wt)
    # ...and the nested worktree is re-exposed AFTER the state tmpfs mask, so files
    # under it are readable. A stale control-root policy emits no such re-bind.
    state_mask = str(project_state_dir(tmp_path).resolve())
    ti = argv.index(state_mask)                     # the --tmpfs <mask> operand
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
    wt = project_state_dir(tmp_path) / "worktrees" / "t9"
    pol = ctx.base_sandbox_policy
    assert pol.worktree == str(wt)
    assert pol.git_worktree_self == str(tmp_path / ".git" / "worktrees" / "t9")
    assert pol.git_worktrees_mask == str(tmp_path / ".git" / "worktrees")
    # The new worktree's own admin dir is writable again...
    assert pol.allows_write(tmp_path / ".git" / "worktrees" / "t9" / "HEAD") is True
    # ...while a sibling admin dir stays masked.
    assert pol.allows_write(tmp_path / ".git" / "worktrees" / "other" / "HEAD") is False


def _set_worktree_setup(root: Path, command: str) -> None:
    """Declare worktree_setup in the MAIN CHECKOUT's project layer."""
    cfg = root / "imp" / "config.yaml"
    cfg.write_text(cfg.read_text() + f"worktree_setup: {json.dumps(command)}\n")


@pytest.mark.anyio
async def test_no_setup_command_runs_when_worktree_setup_is_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.agents.tools.create_and_enter_worktree as cew

    calls: list[dict] = []

    def _record(**kw: object) -> dict[str, object]:
        calls.append(kw)
        return {"exit": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cew, "run_command", _record)
    _seed_control(tmp_path)                       # seeds no worktree_setup
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s1", base_branch="main")
    await get_registry()["create_and_enter_worktree"].async_invoke(
        _ctx(ch, State.load(ch.state_dir)), {})
    assert calls == []                            # no-regression for every other repo


@pytest.mark.anyio
async def test_setup_runs_once_in_the_new_worktree_under_the_quest_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.agents.tools.create_and_enter_worktree as cew

    calls: list[dict] = []

    def _record(**kw: object) -> dict[str, object]:
        calls.append(kw)
        return {"exit": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cew, "run_command", _record)
    _seed_control(tmp_path)
    _set_worktree_setup(tmp_path, "uv sync --all-extras")
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s2", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    ctx.base_sandbox_policy = _active_policy(str(tmp_path))
    await get_registry()["create_and_enter_worktree"].async_invoke(ctx, {})
    wt = project_state_dir(tmp_path) / "worktrees" / "s2"
    assert len(calls) == 1
    assert calls[0]["command"] == "uv sync --all-extras"
    assert calls[0]["cwd"] == str(wt)
    assert calls[0]["timeout_seconds"] == 600
    assert calls[0]["policy"] is ctx.sandbox_policy       # the quest's live policy...
    assert calls[0]["policy"].worktree == str(wt)         # ...already repointed


@pytest.mark.anyio
async def test_setup_is_dispatched_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from typing import Any

    import anyio.to_thread

    from imp.engine.gates import run_command as real_run_command

    dispatched: list[object] = []
    real_run_sync = anyio.to_thread.run_sync

    async def _spy(fn: Any, *args: Any, **kwargs: Any) -> Any:
        dispatched.append(fn)
        return await real_run_sync(fn, *args, **kwargs)

    monkeypatch.setattr(anyio.to_thread, "run_sync", _spy)
    _seed_control(tmp_path)
    _set_worktree_setup(tmp_path, "true")
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s3", base_branch="main")
    await get_registry()["create_and_enter_worktree"].async_invoke(
        _ctx(ch, State.load(ch.state_dir)), {})
    setup = [fn for fn in dispatched
             if getattr(fn, "func", None) is real_run_command]
    assert len(setup) == 1


@pytest.mark.anyio
async def test_setup_reruns_on_a_second_call(tmp_path: Path) -> None:
    _seed_control(tmp_path)
    _set_worktree_setup(tmp_path, "echo ran >> setup.log")
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s4", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    tool = get_registry()["create_and_enter_worktree"]
    await tool.async_invoke(ctx, {})
    await tool.async_invoke(ctx, {})              # materialize() is a no-op; setup is not
    wt = project_state_dir(tmp_path) / "worktrees" / "s4"
    assert (wt / "setup.log").read_text() == "ran\nran\n"


@pytest.mark.anyio
async def test_setup_failure_raises_fatal_with_stdout_then_stderr(
    tmp_path: Path,
) -> None:
    from imp.agents.tools.errors import FatalToolError

    _seed_control(tmp_path)
    cmd = "echo OUT; echo ERR >&2; exit 3"
    _set_worktree_setup(tmp_path, cmd)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s5", base_branch="main")
    with pytest.raises(FatalToolError) as ei:
        await get_registry()["create_and_enter_worktree"].async_invoke(
            _ctx(ch, State.load(ch.state_dir)), {})
    msg = str(ei.value)
    assert msg == f"worktree_setup failed (exit 3): {cmd}\n\nOUT\nERR\n"
    assert msg.index("OUT") < msg.index("ERR")    # stdout first, never interleaved


@pytest.mark.anyio
async def test_setup_failure_reaches_the_provider_fatal_channel(
    tmp_path: Path,
) -> None:
    """A FatalToolError lands on the provider's fatal channel, not a tool result."""
    from imp.providers.codex_mcp import RunState, dispatch_tool

    _seed_control(tmp_path)
    _set_worktree_setup(tmp_path, "exit 7")
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s6", base_branch="main")
    state = RunState(cap=10)
    out = await dispatch_tool(
        tool=get_registry()["create_and_enter_worktree"], args={},
        ctx=_ctx(ch, State.load(ch.state_dir)), state=state)
    assert state.fatal_tool_error is not None
    tool_name, msg = state.fatal_tool_error
    assert tool_name == "create_and_enter_worktree"
    assert msg.startswith("worktree_setup failed (exit 7): exit 7")
    assert "fatal tool error" in out


@pytest.mark.anyio
async def test_setup_timeout_is_reported_through_the_failure_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.agents.tools.create_and_enter_worktree as cew
    from imp.agents.tools.errors import FatalToolError

    monkeypatch.setattr(cew, "WORKTREE_SETUP_TIMEOUT_S", 1)
    _seed_control(tmp_path)
    _set_worktree_setup(tmp_path, "sleep 30")
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s7", base_branch="main")
    with pytest.raises(FatalToolError) as ei:
        await get_registry()["create_and_enter_worktree"].async_invoke(
            _ctx(ch, State.load(ch.state_dir)), {})
    assert str(ei.value) == "worktree_setup timed out after 1s: sleep 30"


def test_setup_ceiling_is_600_seconds() -> None:
    import imp.agents.tools.create_and_enter_worktree as cew

    assert cew.WORKTREE_SETUP_TIMEOUT_S == 600      # fixed; no second config key


@pytest.mark.anyio
async def test_setup_timeout_includes_partial_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import subprocess

    import imp.agents.tools.create_and_enter_worktree as cew
    from imp.agents.tools.errors import FatalToolError

    def _fake_run(**kw: object) -> dict:
        raise subprocess.TimeoutExpired(
            kw["command"], kw["timeout_seconds"],
            output="PARTIAL_OUT", stderr="PARTIAL_ERR",
        )

    monkeypatch.setattr(cew, "run_command", _fake_run)
    _seed_control(tmp_path)
    _set_worktree_setup(tmp_path, "some_hanging_command")
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s8", base_branch="main")
    with pytest.raises(FatalToolError) as ei:
        await get_registry()["create_and_enter_worktree"].async_invoke(
            _ctx(ch, State.load(ch.state_dir)), {})
    msg = str(ei.value)
    assert "PARTIAL_OUT" in msg
    assert "PARTIAL_ERR" in msg


@pytest.mark.anyio
async def test_setup_output_cap_raises_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.engine.gates as gatesmod
    from imp.agents.tools.errors import FatalToolError

    monkeypatch.setattr(gatesmod, "GATE_OUTPUT_CAP_BYTES", 10)
    _seed_control(tmp_path)
    cmd = "printf 'this is more than ten bytes of output'"
    _set_worktree_setup(tmp_path, cmd)
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s9", base_branch="main")
    with pytest.raises(FatalToolError) as ei:
        await get_registry()["create_and_enter_worktree"].async_invoke(
            _ctx(ch, State.load(ch.state_dir)), {})
    assert "output cap exceeded" in str(ei.value)


@pytest.mark.anyio
async def test_no_setup_command_runs_when_worktree_setup_is_false(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.agents.tools.create_and_enter_worktree as cew

    calls: list[dict] = []

    def _record(**kw: object) -> dict[str, object]:
        calls.append(kw)
        return {"exit": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(cew, "run_command", _record)
    _seed_control(tmp_path)
    cfg = tmp_path / "imp" / "config.yaml"
    cfg.write_text(cfg.read_text() + "worktree_setup: false\n")
    ch = Quest.create_draft(tmp_path, title="X", quest_id="s9", base_branch="main")
    ctx = _ctx(ch, State.load(ch.state_dir))
    await get_registry()["create_and_enter_worktree"].async_invoke(ctx, {})
    assert calls == []                            # decided-as-none runs nothing
    assert ctx.project_root == project_state_dir(tmp_path) / "worktrees" / "s9"
