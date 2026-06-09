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
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.change_context = ChangeContext(
        slug="test-change", title="Test Change", project_root=tmp_path,
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


def test_bash_exposes_imp_change_slug(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "echo $IMP_CHANGE_SLUG"}))
    assert out["returncode"] == 0
    assert "test-change" in out["stdout"]


def test_bash_registers_and_clears_running_processes(tmp_path: Path) -> None:
    """On clean exit the process is removed from ctx.running_processes."""
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "true"}))
    assert out["returncode"] == 0
    assert ctx.running_processes == []


def test_bash_exposes_imp_lint_command(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.lint_command = "make lint"
    out = json.loads(_run_bash(ctx, {"command": "echo $IMP_LINT_COMMAND"}))
    assert out["returncode"] == 0
    assert "make lint" in out["stdout"]


def test_bash_no_lint_command_means_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Regression (found by imp-on-imp dogfooding): a stale IMP_LINT_COMMAND in the
    # ambient environment must NOT leak into the subprocess when the ctx has no
    # lint_command. engine_env is authoritative; overlay_engine_env strips the
    # inherited copy. Without that, `{**os.environ, ...}` would pass "make stale"
    # straight through and falsely enable the lint gate.
    monkeypatch.setenv("IMP_LINT_COMMAND", "make stale-lint")
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "echo [$IMP_LINT_COMMAND]"}))
    assert "[]" in out["stdout"]


def test_bash_exposes_imp_test_command(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.test_command = "make test"
    out = json.loads(_run_bash(ctx, {"command": "echo $IMP_TEST_COMMAND"}))
    assert out["returncode"] == 0
    assert "make test" in out["stdout"]


def test_bash_no_test_command_means_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A stale IMP_TEST_COMMAND in the ambient environment must NOT leak into the
    # subprocess when the ctx has no test_command. engine_env is authoritative;
    # overlay_engine_env strips the inherited copy.
    monkeypatch.setenv("IMP_TEST_COMMAND", "make stale-test")
    out = json.loads(_run_bash(_ctx(tmp_path), {"command": "echo [$IMP_TEST_COMMAND]"}))
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
