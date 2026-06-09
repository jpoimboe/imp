# tests/test_fs_tools.py
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import imp.agents.tools.fs as fs_tools  # noqa: F401  triggers registration
from imp.agents.permissions import PermissionDenied
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import ToolAborted
from imp.engine.types import FsRule, SourceLoc


def _allow_all(*ops: str) -> tuple[FsRule, ...]:
    return (FsRule(
        operations=tuple(ops),  # type: ignore[arg-type]
        paths=("**",),
        mode="allow",
        source=SourceLoc(file=Path("t.yaml"), line=1),
    ),)


class _Coord:
    def __init__(self, should_pause: bool = False) -> None:
        self.should_pause = should_pause


def _ctx(tmp_path: Path, *ops: str, coord: object | None = None) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=_allow_all(*ops) if ops else (),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = coord
    return ctx


def _read_file(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["read_file"].async_invoke, ctx, args)


def _edit_file(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["edit_file"].async_invoke, ctx, args)


def test_read_file_returns_numbered_contents(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hello")
    out = _read_file(_ctx(tmp_path, "read"), path="a.txt")
    assert out == "     1\thello"


def test_read_file_giant_single_line_bounded(tmp_path: Path) -> None:
    # The old head-cap test, re-pinned for the paging model: a 200 KB single
    # line is bounded by the per-line 2000-char content cap, not delivered raw.
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * 200_000)  # one line, no newline
    out = _read_file(_ctx(tmp_path, "read"), path="big.bin")
    assert out.endswith("…[truncated]")
    assert len(out) < 3_000  # 2000-char content + prefix + marker, not 200 KB


def test_read_file_aborts_on_pause(tmp_path: Path) -> None:
    from imp.agents.tools.limits import TOOL_OUTPUT_CAP_BYTES
    big = tmp_path / "big.bin"
    big.write_bytes(b"z" * (TOOL_OUTPUT_CAP_BYTES * 4))
    ctx = _ctx(tmp_path, "read", coord=_Coord(should_pause=True))
    with pytest.raises(ToolAborted) as ei:
        _read_file(ctx, path="big.bin")
    assert ei.value.reason == "interrupted"


def test_write_file_creates_file_and_dirs(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "write")
    get_registry()["write_file"].invoke(ctx, {"path": "deep/x.txt", "content": "ok"})
    assert (tmp_path / "deep" / "x.txt").read_text() == "ok"


def test_write_file_default_denied(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)  # no rules; default-deny on write
    with pytest.raises(PermissionDenied):
        get_registry()["write_file"].invoke(ctx, {"path": "x.txt", "content": "x"})


def test_write_file_rejects_escape(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "write")
    with pytest.raises(PermissionDenied):
        get_registry()["write_file"].invoke(ctx, {"path": "../evil.txt", "content": "x"})


def test_edit_file_unique_replacement(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("def foo():\n    return 1\n")
    ctx = _ctx(tmp_path, "read", "write")
    _edit_file(ctx, path="x.py", old="return 1", new="return 2")
    assert (tmp_path / "x.py").read_text() == "def foo():\n    return 2\n"


def test_edit_file_ambiguous_raises(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("x = 1\nx = 1\n")
    ctx = _ctx(tmp_path, "read", "write")
    with pytest.raises(ValueError, match="not unique"):
        _edit_file(ctx, path="x.py", old="x = 1", new="y = 2")


def test_edit_file_missing_raises(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("nothing here\n")
    ctx = _ctx(tmp_path, "read", "write")
    with pytest.raises(ValueError, match="not found"):
        _edit_file(ctx, path="x.py", old="missing", new="x")
