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


def test_bash_exposes_imp_change_id(tmp_path: Path) -> None:
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


def test_bash_caps_runaway_output(tmp_path: Path) -> None:
    # A command that floods stdout far past 50 KB is killed AT the cap; the
    # result is the head + marker, and it is NOT surfaced as an interrupt.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {
        "command": "yes ABCDEFGHIJ | head -c 5000000"}))
    assert "[output truncated at 50 KB]" in out["stdout"]
    assert len(out["stdout"]) < 60_000          # bounded: 50 KB + slop + marker
    assert out["returncode"] == 0               # cap is a NORMAL result, not a kill
    assert ctx.running_processes == []


def test_bash_caps_runaway_stderr(tmp_path: Path) -> None:
    # S2: a stderr-dominant flood must mark stderr (the stream that crossed the
    # cap), NOT leave a truncation marker on an empty stdout.
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {
        "command": "yes ABCDEFGHIJ | head -c 5000000 1>&2"}))
    assert "[output truncated at 50 KB]" in out["stderr"]
    assert "[output truncated" not in out["stdout"]
    assert len(out["stderr"]) < 60_000          # bounded: 50 KB + slop + marker
    assert out["returncode"] == 0               # cap is a NORMAL result, not a kill
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
