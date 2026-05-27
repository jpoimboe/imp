# tests/test_bash_tool.py
from __future__ import annotations

import json
from pathlib import Path

import anyio

import imp.agents.tools.bash  # noqa: F401
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext


def _ctx(tmp_path: Path) -> ToolContext:
    artifacts = tmp_path / "_artifacts"
    artifacts.mkdir()
    return ToolContext(
        project_root=tmp_path,
        artifacts_dir=artifacts,
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )


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


def test_bash_registers_and_clears_running_processes(tmp_path: Path) -> None:
    """On clean exit the process is removed from ctx.running_processes."""
    ctx = _ctx(tmp_path)
    out = json.loads(_run_bash(ctx, {"command": "true"}))
    assert out["returncode"] == 0
    assert ctx.running_processes == []


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
