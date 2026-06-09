# tests/test_nav_tools.py
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import imp.agents.tools.nav  # noqa: F401
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import ToolAborted


class _Coord:
    def __init__(self, should_pause: bool = False) -> None:
        self.should_pause = should_pause


def _ctx(tmp_path: Path, coord: object | None = None) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = coord
    return ctx


def _grep(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["grep"].async_invoke, ctx, args)


def _glob(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["glob"].async_invoke, ctx, args)


def test_glob_finds_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "b.py").write_text("")
    (tmp_path / "c.txt").write_text("")
    out = _glob(_ctx(tmp_path), pattern="*.py")
    assert "a.py" in out
    assert "b.py" in out
    assert "c.txt" not in out


def test_glob_recursive(tmp_path: Path) -> None:
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "x.md").write_text("")
    out = _glob(_ctx(tmp_path), pattern="**/*.md")
    assert "sub/x.md" in out


def test_grep_finds_match(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("hello world\nfoo bar\n")
    (tmp_path / "b.py").write_text("nothing\n")
    out = _grep(_ctx(tmp_path), pattern="hello", path=".")
    assert "a.py" in out
    assert "b.py" not in out


def test_ls_lists_dir(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("")
    (tmp_path / "sub").mkdir()
    out = get_registry()["ls"].invoke(_ctx(tmp_path), {"path": "."})
    assert "a.py" in out
    assert "sub" in out


def test_grep_excludes_imp_and_git(tmp_path: Path) -> None:
    (tmp_path / "code.py").write_text("SECRET token here\n")
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "events.jsonl").write_text("SECRET in the event log\n")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("SECRET in git config\n")

    out = _grep(_ctx(tmp_path), pattern="SECRET", path=".")

    assert "code.py" in out
    assert ".imp" not in out
    assert ".git" not in out


def test_grep_bounds_a_single_giant_line(tmp_path: Path) -> None:
    # The incident shape: one enormous single line with no newline. Here the
    # operative bound is MATCH_LINE_CAP_BYTES (each matched line is byte-truncated)
    # — the 4 MB file is under FILE_READ_CAP_BYTES (5 MB) so it is read whole; the
    # *read* ceiling itself is unit-tested separately in test_tool_limits.py.
    big = tmp_path / "huge.log"
    big.write_bytes(b"NEEDLE" + b"x" * 4_000_000)  # one line, 4 MB, no newline
    out = _grep(_ctx(tmp_path), pattern="NEEDLE", output_mode="content")
    # The matched line is byte-truncated, not a 4 MB result.
    assert "NEEDLE" in out
    assert len(out) < 10_000


def test_grep_caps_total_output(tmp_path: Path) -> None:
    # Many matching lines -> output stops at the head cap with the marker.
    f = tmp_path / "many.txt"
    f.write_text("\n".join(f"match line {i}" for i in range(200_000)) + "\n")
    out = _grep(_ctx(tmp_path), pattern="match", output_mode="content")
    assert "[output truncated at 50 KB]" in out
    assert len(out) < 60_000


def test_grep_aborts_promptly_on_pause(tmp_path: Path) -> None:
    # C-i: should_pause set BEFORE the call -> aborts at the first checkpoint.
    for i in range(50):
        (tmp_path / f"f{i}.txt").write_text("hay\nNEEDLE\nhay\n")
    ctx = _ctx(tmp_path, coord=_Coord(should_pause=True))
    with pytest.raises(ToolAborted) as ei:
        _grep(ctx, pattern="NEEDLE")
    assert ei.value.reason == "interrupted"


def test_grep_still_finds_normal_matches(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def foo():\n    pass\n")
    out = _grep(_ctx(tmp_path), pattern=r"def foo", output_mode="content")
    assert "a.py:1:def foo():" in out


def test_glob_excludes_imp_and_git(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("x")
    (tmp_path / ".imp").mkdir()
    (tmp_path / ".imp" / "events.jsonl").write_text("x")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("x")

    out = _glob(_ctx(tmp_path), pattern="**/*")

    assert "a.py" in out
    assert ".imp" not in out
    assert ".git" not in out


def test_glob_aborts_promptly_on_pause(tmp_path: Path) -> None:
    for i in range(50):
        (tmp_path / f"f{i}.txt").write_text("x")
    ctx = _ctx(tmp_path, coord=_Coord(should_pause=True))
    with pytest.raises(ToolAborted) as ei:
        _glob(ctx, pattern="**/*")
    assert ei.value.reason == "interrupted"


# --- Containment / escape-guard tests (Task A2, spec H1) ---

from imp.agents.permissions import PermissionDenied  # noqa: E402


def test_grep_rejects_parent_escape(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        anyio.run(get_registry()["grep"].async_invoke, ctx,
                  {"pattern": "root", "path": "../../etc"})


def test_grep_rejects_absolute_project_escape(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        anyio.run(get_registry()["grep"].async_invoke, ctx,
                  {"pattern": "root", "path": "/project/../../etc"})


def test_ls_rejects_parent_escape(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        get_registry()["ls"].invoke(ctx, {"path": "../.."})


def test_glob_rejects_absolute_pattern(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        anyio.run(get_registry()["glob"].async_invoke, ctx,
                  {"pattern": "/project/**/*.py"})


def test_glob_rejects_dotdot_pattern(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        anyio.run(get_registry()["glob"].async_invoke, ctx,
                  {"pattern": "../../etc/**"})


def test_grep_reports_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("needle here\n")
    ctx = _ctx(tmp_path)
    out = anyio.run(get_registry()["grep"].async_invoke, ctx,
                    {"pattern": "needle", "path": ".", "output_mode": "content"})
    assert out.startswith("a.txt:")          # relative, no /project prefix
