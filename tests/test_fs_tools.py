# tests/test_fs_tools.py
from __future__ import annotations

from pathlib import Path

import pytest

import imp.agents.tools.fs as fs_tools  # noqa: F401  triggers registration
from imp.agents.permissions import PermissionDenied
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.engine.types import FsRule, SourceLoc


def _allow_all(*ops: str) -> tuple[FsRule, ...]:
    return (FsRule(
        operations=tuple(ops),  # type: ignore[arg-type]
        paths=("/project/**",),
        mode="allow",
        source=SourceLoc(file=Path("t.yaml"), line=1),
    ),)


def _ctx(tmp_path: Path, *ops: str) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
        fs_permissions=_allow_all(*ops) if ops else (),
        events_path=tmp_path / "events.jsonl",
    )


def test_read_file_returns_contents(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    out = get_registry()["read_file"].invoke(_ctx(tmp_path, "read"), {"path": "/project/a.txt"})
    assert out == "hello"


def test_read_file_scratch_returns_dict_value(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "read")
    ctx.scratch["/scratch/x"] = "from-memory"
    out = get_registry()["read_file"].invoke(ctx, {"path": "/scratch/x"})
    assert out == "from-memory"


def test_write_file_creates_file_and_dirs(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "write")
    get_registry()["write_file"].invoke(ctx, {"path": "/project/deep/x.txt", "content": "ok"})
    assert (tmp_path / "deep" / "x.txt").read_text() == "ok"


def test_write_file_default_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)  # no rules; default-deny on write
    with pytest.raises(PermissionDenied):
        get_registry()["write_file"].invoke(ctx, {"path": "/project/x.txt", "content": "x"})


def test_edit_file_unique_replacement(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("def foo():\n    return 1\n")
    ctx = _ctx(tmp_path, "read", "write")
    get_registry()["edit_file"].invoke(ctx, {
        "path": "/project/x.py", "old": "return 1", "new": "return 2",
    })
    assert (tmp_path / "x.py").read_text() == "def foo():\n    return 2\n"


def test_edit_file_ambiguous_raises(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("x = 1\nx = 1\n")
    ctx = _ctx(tmp_path, "read", "write")
    with pytest.raises(ValueError, match="not unique"):
        get_registry()["edit_file"].invoke(ctx, {
            "path": "/project/x.py", "old": "x = 1", "new": "y = 2",
        })


def test_edit_file_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("nothing here\n")
    ctx = _ctx(tmp_path, "read", "write")
    with pytest.raises(ValueError, match="not found"):
        get_registry()["edit_file"].invoke(ctx, {
            "path": "/project/x.py", "old": "missing", "new": "x",
        })
