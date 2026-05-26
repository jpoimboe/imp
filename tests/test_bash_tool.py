# tests/test_bash_tool.py
from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
import imp.agents.tools.bash  # noqa: F401


def _ctx(tmp_path: Path) -> ToolContext:
    artifacts = tmp_path / "_artifacts"
    artifacts.mkdir()
    return ToolContext(
        project_root=tmp_path,
        artifacts_dir=artifacts,
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )


def test_bash_runs_simple_command(tmp_path: Path) -> None:
    raw = get_registry()["bash"].invoke(_ctx(tmp_path), {"command": "echo hi"})
    out = json.loads(raw)
    assert out["returncode"] == 0
    assert "hi" in out["stdout"]


def test_bash_exposes_returncode(tmp_path: Path) -> None:
    raw = get_registry()["bash"].invoke(_ctx(tmp_path), {"command": "exit 7"})
    out = json.loads(raw)
    assert out["returncode"] == 7


def test_bash_default_cwd_is_project_root(tmp_path: Path) -> None:
    raw = get_registry()["bash"].invoke(_ctx(tmp_path), {"command": "pwd"})
    out = json.loads(raw)
    assert str(tmp_path) in out["stdout"]


def test_bash_explicit_cwd(tmp_path: Path) -> None:
    sub = tmp_path / "sub"
    sub.mkdir()
    raw = get_registry()["bash"].invoke(
        _ctx(tmp_path), {"command": "pwd", "cwd": str(sub)}
    )
    out = json.loads(raw)
    assert str(sub) in out["stdout"]
