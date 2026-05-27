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
