# tests/test_bash_tool.py
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.runner import ChangeContext
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import ToolAborted


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl",
    )
    ctx.change_context = ChangeContext(
        id="test01", title="Test Change", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow",
    )
    return ctx


def _run_bash(ctx, args):
    return anyio.run(get_registry()["bash"].async_invoke, ctx, args)


def test_bash_runs_simple_command(tmp_path: Path) -> None:
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "echo hi"}))
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]


def test_bash_exposes_returncode(tmp_path: Path) -> None:
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "exit 7"}))
    assert out["returncode"] == 7


def test_bash_default_cwd_is_project_root(tmp_path: Path) -> None:
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "pwd"}))
    assert str(tmp_path) in out["stdout"]


def test_bash_explicit_cwd(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "pwd", "cwd": str(sub)}))
    assert str(sub) in out["stdout"]


def test_bash_exposes_imp_project_root(tmp_path: Path) -> None:
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "echo $IMP_PROJECT_ROOT"}))
    assert out["returncode"] == 0
    assert str(tmp_path) in out["stdout"]


def test_bash_exposes_imp_quest_id(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "echo $IMP_CHANGE_ID"}))
    assert out["returncode"] == 0
    assert "test01" in out["stdout"]


def test_bash_registers_and_clears_running_processes(tmp_path: Path) -> None:
    """On clean exit the process is removed from ctx.running_processes."""
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "true"}))
    assert out["returncode"] == 0
    assert ctx.running_processes == []


def test_bash_exposes_imp_test_cmd_fast(tmp_path: Path) -> None:
    from imp.engine.env import TestGates
    ctx = _ctx(tmp_path)
    ctx.test_gates = TestGates(fast="make check")
    out = json.loads(_run_bash(ctx, {"command": "echo $IMP_TEST_CMD_FAST"}))
    assert out["returncode"] == 0
    assert "make check" in out["stdout"]


def test_bash_no_test_gates_means_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression: a stale ambient IMP_TEST_CMD_FAST must NOT leak into the
    # subprocess when ctx has no test_gates. engine_env is authoritative;
    # overlay_engine_env strips the inherited copy.
    monkeypatch.setenv("IMP_TEST_CMD_FAST", "make stale")
    out = json.loads(_run_bash(_ctx(tmp_path),
                               {"command": "echo [$IMP_TEST_CMD_FAST]"}))
    assert "[]" in out["stdout"]


def test_bash_terminates_on_runner_interrupt(tmp_path: Path) -> None:
    """terminate_running_processes mid-bash drains the registry and returns
    the killed processes; bash's returncode reflects the signal."""
    from imp.agents.tools.context import terminate_running_processes

    ctx = _ctx(tmp_path)

    async def _scenario():
        async with anyio.create_task_group() as tg:
            killed_holder: dict[str, list] = {}

            async def _runner():
                out = json.loads(
                    await get_registry()["bash"].async_invoke(
                        ctx, {"command": "sleep 10"},
                    ),
                )
                killed_holder["out"] = out

            async def _killer():
                # Wait briefly so bash has time to register its proc.
                await anyio.sleep(0.2)
                killed = await terminate_running_processes(ctx, timeout=0.5)
                killed_holder["killed"] = killed

            tg.start_soon(_runner)
            tg.start_soon(_killer)

        # bash returned with a signal-killed returncode (< 0).
        assert killed_holder["out"]["returncode"] < 0
        # Registry is drained.
        assert ctx.running_processes == []
        # The killer reported at least one process.
        assert len(killed_holder["killed"]) >= 1

    anyio.run(_scenario)


def test_bash_over_cap_spills_full_output_and_previews_head(tmp_path: Path) -> None:
    # Natural completion emitting > 50 KB (but < 5 MB): the FULL stream is
    # spilled to tool-results/ (recoverable), the displayed stdout is the 50 KB
    # head, a notice carries the spill path, and returncode is a normal 0.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {
        "command": "yes ABCDEFGHIJ | head -c 100000"}))
    assert out["returncode"] == 0
    assert len(out["stdout"]) == 50_000              # 50 KB head display
    assert "[output truncated" not in out["stdout"]  # marker replaced by notice
    assert "tool-results" in out["notice"]
    assert "read with read_file offset=1" in out["notice"]
    assert ctx.running_processes == []
    # The spill file holds the FULL 100 KB stream, not the truncated head.
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    data = spilled[0].read_bytes()
    assert len(data) == 100_000
    assert data.startswith(b"ABCDEFGHIJ")


def test_bash_over_cap_spill_labels_stdout_and_stderr(tmp_path: Path) -> None:
    # When stderr is non-empty, the single spill file carries BOTH tails under
    # labeled sections so one read_file recovers each.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {
        "command": "yes OUT | head -c 40000; yes ERR | head -c 40000 1>&2"}))
    assert "notice" in out
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    content = spilled[0].read_text(encoding="utf-8")
    assert "=== stdout ===" in content
    assert "=== stderr ===" in content
    assert "OUT" in content and "ERR" in content


def test_bash_caps_runaway_output(tmp_path: Path) -> None:
    # A command that floods stdout past the 5 MB kill ceiling is killed AT the
    # ceiling; the displayed stdout is the 50 KB head + a spill notice (the old
    # inline marker is gone), and it is NOT surfaced as an interrupt.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "yes ABCDEFGHIJ"}))
    assert "[output truncated" not in out["stdout"]
    assert "notice" in out
    assert (tmp_path / "tool-results").exists()
    assert len(out["stdout"]) == 50_000
    assert out["returncode"] == 0                    # cap is a NORMAL result
    assert ctx.running_processes == []


def test_bash_caps_runaway_stderr(tmp_path: Path) -> None:
    # A stderr-dominant flood past the ceiling truncates the displayed stderr to
    # the 50 KB head and emits a spill notice; no inline marker on either stream.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "yes ABCDEFGHIJ 1>&2"}))
    assert "[output truncated" not in out["stderr"]
    assert "[output truncated" not in out["stdout"]
    assert "notice" in out
    assert (tmp_path / "tool-results").exists()
    assert len(out["stderr"]) == 50_000
    assert out["returncode"] == 0                    # cap is a NORMAL result
    assert ctx.running_processes == []


def test_bash_wall_clock_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # B.1: a command sleeping past the wall-clock is killed and raises ToolAborted
    # ('timed-out'), asserted at the TOOL layer. Inject a short timeout.
    import imp.agents.tools.bash as bash_mod
    from imp.agents.tools.limits import ToolAborted

    monkeypatch.setattr(bash_mod, "TOOL_WALLCLOCK_TIMEOUT_S", 0.3)
    ctx = _ctx(tmp_path)
    with pytest.raises(ToolAborted) as ei:
        _run_bash(ctx, {"command": "sleep 30"})
    assert ei.value.reason == "timed-out"
    assert ctx.running_processes == []


def test_bash_normal_output_uncapped(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "echo hello"}))
    assert out["stdout"].strip() == "hello"
    assert "[output truncated" not in out["stdout"]
    assert "notice" not in out                       # <= 50 KB is unchanged
    assert out["returncode"] == 0


def test_bash_relative_cd_works(tmp_path: Path) -> None:
    sub = tmp_path / "src"
    sub.mkdir()
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "cd src && pwd"}))
    assert out["returncode"] == 0
    assert str(sub) in out["stdout"]


def test_bash_wraps_with_active_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.agents.tools.bash as bashmod
    from imp.engine.sandbox import SandboxPolicy

    captured: dict[str, object] = {}

    async def fake_open_process(argv: list[str], **kw: object) -> object:
        captured["argv"] = argv
        raise RuntimeError("stop after capture")   # we only assert the argv

    monkeypatch.setattr(bashmod.anyio, "open_process", fake_open_process)
    ctx = _ctx(tmp_path)
    ctx.sandbox_policy = SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(),
        setenv=(("PATH", "/usr/bin"),), network=True, note="",
    )
    # _run_bash propagates the RuntimeError out via anyio.run
    with pytest.raises(RuntimeError):
        _run_bash(ctx, {"command": "echo hi"})
    assert captured["argv"][0] == "bwrap"
    assert captured["argv"][-3:] == ["sh", "-c", "echo hi"]


def test_bash_bare_when_no_policy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import imp.agents.tools.bash as bashmod

    captured: dict[str, object] = {}

    async def fake_open_process(argv: list[str], **kw: object) -> object:
        captured["argv"] = argv
        raise RuntimeError("stop")

    monkeypatch.setattr(bashmod.anyio, "open_process", fake_open_process)
    # no sandbox_policy set on ctx — should use bare sh -c
    with pytest.raises(RuntimeError):
        _run_bash(_ctx(tmp_path), {"command": "echo hi"})
    assert captured["argv"] == ["sh", "-c", "echo hi"]


def test_bash_pending_pause_never_spawns_child(tmp_path: Path) -> None:
    # §4 pre-spawn contract: a pending pause aborts before the child spawns.
    class _PausedCoord:
        should_pause = True

    ctx = _ctx(tmp_path)
    ctx.coordinator = _PausedCoord()
    sentinel = tmp_path / "ran"
    with pytest.raises(ToolAborted) as ei:
        _run_bash(ctx, {"command": f"touch {sentinel}"})
    assert ei.value.reason == "interrupted"
    assert not sentinel.exists()          # child never spawned
    assert ctx.running_processes == []


def test_bash_editor_git_op_completes_without_blocking(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A hostile ambient editor that exits nonzero must NOT reach the agent's git
    # subprocess: engine_env forces GIT_EDITOR=true and overlay_engine_env strips
    # the ambient copy, so `git commit --amend` (no -m) keeps the prepared message
    # and exits 0 instead of aborting on (or blocking in) the editor.
    monkeypatch.setenv("GIT_EDITOR", "false")
    monkeypatch.setenv("EDITOR", "false")
    monkeypatch.setenv("VISUAL", "false")
    ctx = _ctx(tmp_path)
    setup = _run_bash(ctx, {"command": (
        "git init -q && git config user.email a@b.c && git config user.name t "
        "&& git commit -q --allow-empty -m seed")})
    assert json.loads(setup)["returncode"] == 0

    amended = json.loads(_run_bash(ctx, {"command": "git commit --amend --allow-empty"}))
    assert amended["returncode"] == 0

    # Observable consequence: the amend kept the single seed commit intact.
    log = json.loads(_run_bash(ctx, {"command": "git log --format=%s"}))
    assert log["stdout"].split() == ["seed"]


def test_bash_midcommand_pause_kills_and_returns_tombstone(tmp_path: Path) -> None:
    # §4 mid-command contract: a pause landing WHILE a command runs kills the
    # child's group and returns the terminated-by-pause tombstone (status:
    # killed, kill_reason: interrupted) — the same shape
    # terminate_background_processes writes — instead of blocking to the wall
    # clock. fail_after(10) makes the "poller absent" regression a fast failure
    # rather than a 30s hang.
    class _Coord:
        should_pause = False

    coord = _Coord()
    ctx = _ctx(tmp_path)
    ctx.coordinator = coord
    holder: dict[str, dict] = {}

    async def _scenario() -> None:
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                async def _runner() -> None:
                    holder["out"] = json.loads(
                        await get_registry()["bash"].async_invoke(
                            ctx, {"command": "sleep 30"}))

                async def _pauser() -> None:
                    await anyio.sleep(0.3)      # let bash spawn + register first
                    coord.should_pause = True

                tg.start_soon(_runner)
                tg.start_soon(_pauser)

    anyio.run(_scenario)
    out = holder["out"]
    assert out["status"] == "killed"
    assert out["kill_reason"] == "interrupted"
    assert out["returncode"] < 0               # real signal kill, not massaged to 0
    assert ctx.running_processes == []


def test_bash_midcommand_pause_over_cap_spills_tombstone(tmp_path: Path) -> None:
    # A pause landing mid-command after the command already emitted > 50 KB must
    # spill the full output like the normal path: the tombstone keeps its
    # status/kill_reason but its displayed stdout is the 50 KB head + a notice,
    # not up to the 5 MB kill ceiling inline.
    class _Coord:
        should_pause = False

    coord = _Coord()
    ctx = _ctx(tmp_path)
    ctx.coordinator = coord
    holder: dict[str, dict] = {}

    async def _scenario() -> None:
        with anyio.fail_after(10):
            async with anyio.create_task_group() as tg:
                async def _runner() -> None:
                    holder["out"] = json.loads(
                        await get_registry()["bash"].async_invoke(
                            ctx, {"command":
                                  "yes ABCDEFGHIJ | head -c 100000; sleep 30"}))

                async def _pauser() -> None:
                    await anyio.sleep(0.5)      # let the 100 KB flush first
                    coord.should_pause = True

                tg.start_soon(_runner)
                tg.start_soon(_pauser)

    anyio.run(_scenario)
    out = holder["out"]
    assert out["status"] == "killed"
    assert out["kill_reason"] == "interrupted"
    assert len(out["stdout"]) == 50_000              # 50 KB head display
    assert "notice" in out
    spilled = list((tmp_path / "tool-results").iterdir())
    assert len(spilled) == 1
    assert len(spilled[0].read_bytes()) == 100_000   # FULL output recoverable
    assert ctx.running_processes == []


def test_bash_with_coordinator_no_pause_completes_normally(tmp_path: Path) -> None:
    # Regression: with a coordinator present but NO pause, the poller must stay
    # out of the way — the command completes normally and returns the ordinary
    # {stdout, stderr, returncode} result (no tombstone fields), and the call
    # returns (the poller is cancelled, not left holding the task group open).
    class _Coord:
        should_pause = False

    ctx = _ctx(tmp_path)
    ctx.coordinator = _Coord()
    out = json.loads(_run_bash(ctx, {"command": "echo hi"}))
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]
    assert "status" not in out                 # normal result, not a tombstone
    assert "kill_reason" not in out
    assert ctx.running_processes == []
