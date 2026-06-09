# tests/test_read_file_paging.py
"""read_file paging/line-numbering (tool-ergonomics spec). Notice skeleton
strings are NORMATIVE — asserted verbatim; wording changes go through the
spec, not the implementation."""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest

import imp.agents.tools.fs as fs_tools  # noqa: F401  triggers registration
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.limits import MATCH_LINE_CAP_BYTES
from imp.engine.types import FsRule, SourceLoc


def _ctx(tmp_path: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(FsRule(
            operations=("read",), paths=("**",), mode="allow",
            source=SourceLoc(file=Path("t.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = None
    return ctx


def _read(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["read_file"].async_invoke, ctx, args)


def _num(n: int, content: str) -> str:
    # cat -n style: lineno right-aligned in a 6-column field before a tab.
    # Deliberately NOT asserted for >999,999-line files — the field widens
    # naturally there and the 6-column width is not universal.
    return f"{n:>6}\t{content}"


def test_basic_window_is_line_numbered(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("alpha\nbeta\ngamma\n")
    out = _read(_ctx(tmp_path), path="a.txt")
    # Complete file, window covers EOF: ends SILENTLY — no notice line.
    assert out == f"{_num(1, 'alpha')}\n{_num(2, 'beta')}\n{_num(3, 'gamma')}"


def test_offset_and_limit_slice_with_notice(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("".join(f"line{i}\n" for i in range(1, 11)))
    out = _read(_ctx(tmp_path), path="a.txt", offset=3, limit=2)
    assert out == (f"{_num(3, 'line3')}\n{_num(4, 'line4')}\n"
                   "[showing lines 3-4 of 10; continue with offset=5]")


def test_window_overshooting_eof_is_silent(tmp_path: Path) -> None:
    # offset + limit - 1 >= total (overshoot): NO notice at all. The >=
    # matters — an exact-fit == predicate would emit a bogus
    # continue-instruction here and invite a wasted empty-window call.
    (tmp_path / "a.txt").write_text("".join(f"l{i}\n" for i in range(1, 51)))
    out = _read(_ctx(tmp_path), path="a.txt", offset=10, limit=2000)
    assert out.endswith(_num(50, "l50"))
    assert "[" not in out


def test_window_exact_fit_at_eof_is_silent(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb\nc\n")
    out = _read(_ctx(tmp_path), path="a.txt", offset=2, limit=2)
    assert out == f"{_num(2, 'b')}\n{_num(3, 'c')}"


def test_unterminated_final_line_is_counted(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb")  # no trailing newline
    out = _read(_ctx(tmp_path), path="a.txt")
    assert out == f"{_num(1, 'a')}\n{_num(2, 'b')}"


def test_terminal_newline_phantom_not_counted(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb\n")
    out = _read(_ctx(tmp_path), path="a.txt")
    assert out == f"{_num(1, 'a')}\n{_num(2, 'b')}"  # 2 lines, never 3


def test_formfeed_numbers_by_newline_count(tmp_path: Path) -> None:
    # splitlines() would call this 4 lines; \n-counting says 2.
    (tmp_path / "ff.txt").write_text("a\fb\nc\vd")
    out = _read(_ctx(tmp_path), path="ff.txt")
    assert out == f"{_num(1, 'a\fb')}\n{_num(2, 'c\vd')}"


def test_empty_file_sentinel(tmp_path: Path) -> None:
    (tmp_path / "e.txt").write_text("")
    # Exactly the notice — never a bare empty string (indistinguishable from
    # a failed read).
    assert _read(_ctx(tmp_path), path="e.txt") == "[empty file]"


def test_empty_file_wins_over_past_eof(tmp_path: Path) -> None:
    (tmp_path / "e.txt").write_text("")
    assert _read(_ctx(tmp_path), path="e.txt", offset=5) == "[empty file]"


def test_offset_past_eof_notice(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("a\nb\nc\n")
    out = _read(_ctx(tmp_path), path="a.txt", offset=7)
    assert out == "[offset 7 is past the end of the file (3 lines)]"


def test_long_line_truncated_prefix_excluded_from_content_cap(tmp_path: Path) -> None:
    long_line = "y" * (MATCH_LINE_CAP_BYTES + 50)
    (tmp_path / "l.txt").write_text(long_line + "\nshort\n")
    out = _read(_ctx(tmp_path), path="l.txt")
    line1, line2 = out.split("\n")
    # Content capped at 2000 CHARS with grep's marker; the {lineno:>6}\t
    # prefix does NOT count toward the 2000 (it bounds what a line may
    # contribute; the 50 KB budget bounds what the agent receives).
    assert line1 == _num(1, "y" * MATCH_LINE_CAP_BYTES + "…[truncated]")
    assert line2 == _num(2, "short")


@pytest.mark.parametrize("bad", [0, -1])
def test_invalid_offset_rejected(tmp_path: Path, bad: int) -> None:
    (tmp_path / "a.txt").write_text("x\n")
    with pytest.raises(ValueError, match="offset"):
        _read(_ctx(tmp_path), path="a.txt", offset=bad)


@pytest.mark.parametrize("bad", [0, -3])
def test_invalid_limit_rejected(tmp_path: Path, bad: int) -> None:
    (tmp_path / "a.txt").write_text("x\n")
    with pytest.raises(ValueError, match="limit"):
        _read(_ctx(tmp_path), path="a.txt", limit=bad)


def test_missing_file_error_text(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="no such file: nope.txt"):
        _read(_ctx(tmp_path), path="nope.txt")


def test_directory_error_text(tmp_path: Path) -> None:
    (tmp_path / "d").mkdir()
    with pytest.raises(IsADirectoryError, match="is a directory: d"):
        _read(_ctx(tmp_path), path="d")
