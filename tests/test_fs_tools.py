# tests/test_fs_tools.py
from __future__ import annotations

import hashlib
import os
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.fs as fs_tools
import imp.agents.tools.lines as lines_mod
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext
from imp.agents.tools.errors import PermissionDenied
from imp.agents.tools.limits import ToolAborted
from imp.engine.sandbox import SandboxPolicy


class _Coord:
    def __init__(self, should_pause: bool = False) -> None:
        self.should_pause = should_pause


def _ctx(tmp_path: Path, *ops: str, coord: object | None = None) -> ToolContext:
    ctx = ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl",
    )
    ctx.coordinator = coord
    return ctx


def _read_file(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["read_file"].async_invoke, ctx, args)


def _edit_file(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["edit_file"].async_invoke, ctx, args)


def _write_file(ctx: ToolContext, **args: object) -> str:
    return anyio.run(get_registry()["write_file"].async_invoke, ctx, args)


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


def test_write_file_allowed_by_loose_default(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)  # no policy → bash parity, write allowed
    get_registry()["write_file"].invoke(ctx, {"path": "x.txt", "content": "x"})
    assert (tmp_path / "x.txt").read_text() == "x"


def test_write_file_denied_under_readonly_worktree(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.sandbox_policy = SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), setenv=(), network=True, note="",
        readonly_overlays=(str(tmp_path),),
    )
    with pytest.raises(PermissionDenied):
        get_registry()["write_file"].invoke(ctx, {"path": "x.txt", "content": "x"})


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


def test_write_then_read_out_of_worktree_unsandboxed(tmp_path: Path) -> None:
    # sandbox_policy is None (default) → bash parity: out-of-worktree writes ok.
    ctx = _ctx(tmp_path, "read", "write")
    out = tmp_path.parent / "imp_outside.txt"
    try:
        msg = _write_file(ctx, path=str(out), content="hi there")
        assert out.read_text() == "hi there"
        assert "hi there" in _read_file(ctx, path=str(out))
        assert str(out) in msg
    finally:
        if out.exists():
            out.unlink()


def test_write_out_of_worktree_denied_when_sandboxed_and_uncovered(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "read", "write")
    ctx.sandbox_policy = SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=(), secret_files=(), setenv=(), network=True, note="",
    )
    with pytest.raises(PermissionDenied):
        _write_file(ctx, path="/usr/lib/imp_should_not_write.txt", content="x")


def test_read_file_records_full_read(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_bytes(b"hello world\n")
    ctx = _ctx(tmp_path, "read")
    _read_file(ctx, path="a.txt")
    rec = ctx.read_state[ctx.resolve_path("a.txt")]
    assert rec.full is True
    assert rec.content_sha256 == hashlib.sha256(b"hello world\n").hexdigest()


def test_read_file_records_partial_read_as_not_full(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Shrink BOTH the fs read cap (what read_file passes to the bounded reader)
    # and the lines threshold (what classifies the file as cap-truncated) so a
    # 16-byte file reads as a partial, cap-truncated read.
    monkeypatch.setattr(fs_tools, "FILE_READ_CAP_BYTES", 8)
    monkeypatch.setattr(lines_mod, "FILE_READ_CAP_BYTES", 8)
    (tmp_path / "big.txt").write_bytes(b"0123456789abcdef")  # 16 bytes > cap 8
    ctx = _ctx(tmp_path, "read")
    _read_file(ctx, path="big.txt")
    rec = ctx.read_state[ctx.resolve_path("big.txt")]
    assert rec.full is False
    assert rec.content_sha256 == hashlib.sha256(b"01234567").hexdigest()


def test_write_file_refused_when_existing_and_unread(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("old")
    ctx = _ctx(tmp_path, "read", "write")
    with pytest.raises(ValueError, match="hasn't been read this step"):
        _write_file(ctx, path="p.md", content="new")


def test_write_file_allowed_for_nonexistent_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "write")
    _write_file(ctx, path="new.md", content="hi")
    assert (tmp_path / "new.md").read_text() == "hi"


def test_write_file_allowed_after_read_unchanged(tmp_path: Path) -> None:
    (tmp_path / "p.md").write_text("old")
    ctx = _ctx(tmp_path, "read", "write")
    _read_file(ctx, path="p.md")
    _write_file(ctx, path="p.md", content="new")
    assert (tmp_path / "p.md").read_text() == "new"


def test_write_file_refused_when_content_changed_externally(tmp_path: Path) -> None:
    p = tmp_path / "p.md"
    p.write_text("old")
    ctx = _ctx(tmp_path, "read", "write")
    _read_file(ctx, path="p.md")
    rec = ctx.read_state[ctx.resolve_path("p.md")]
    p.write_text("changed by someone else")
    os.utime(p, (rec.mtime + 10, rec.mtime + 10))
    with pytest.raises(ValueError, match="changed since you read it"):
        _write_file(ctx, path="p.md", content="new")


def test_second_write_to_same_path_allowed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, "read", "write")
    _write_file(ctx, path="p.md", content="first")   # nonexistent -> allowed; record refreshed
    _write_file(ctx, path="p.md", content="second")  # record present, mtime matches -> allowed
    assert (tmp_path / "p.md").read_text() == "second"


def test_symlink_read_then_write_target_allowed(tmp_path: Path) -> None:
    target = tmp_path / "target.md"
    target.write_text("data")
    (tmp_path / "link.md").symlink_to(target)
    ctx = _ctx(tmp_path, "read", "write")
    _read_file(ctx, path="link.md")                    # records under resolved == target
    _write_file(ctx, path="target.md", content="new")  # shared resolved key -> allowed
    assert target.read_text() == "new"


def test_partial_read_then_write_unchanged_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs_tools, "FILE_READ_CAP_BYTES", 8)
    monkeypatch.setattr(lines_mod, "FILE_READ_CAP_BYTES", 8)
    p = tmp_path / "big.md"
    p.write_bytes(b"0123456789abcdef")
    ctx = _ctx(tmp_path, "read", "write")
    _read_file(ctx, path="big.md")
    assert ctx.read_state[ctx.resolve_path("big.md")].full is False
    _write_file(ctx, path="big.md", content="new")  # mtime not advanced -> allowed
    assert (tmp_path / "big.md").read_text() == "new"


def test_partial_read_then_write_after_external_change_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(fs_tools, "FILE_READ_CAP_BYTES", 8)
    monkeypatch.setattr(lines_mod, "FILE_READ_CAP_BYTES", 8)
    p = tmp_path / "big.md"
    p.write_bytes(b"0123456789abcdef")
    ctx = _ctx(tmp_path, "read", "write")
    _read_file(ctx, path="big.md")
    rec = ctx.read_state[ctx.resolve_path("big.md")]
    p.write_bytes(b"0123456789ABCDEF")  # same length, different bytes
    os.utime(p, (rec.mtime + 10, rec.mtime + 10))
    with pytest.raises(ValueError, match="changed since you read it"):
        _write_file(ctx, path="big.md", content="new")


def test_write_allowed_when_mtime_bumped_but_bytes_identical(tmp_path: Path) -> None:
    # \xff (decodes via errors="replace" to U+FFFD) stays byte-identical across
    # the touch, so the guard's raw-bytes compare must allow the write; a
    # decoded-text hasher on the gate side would mismatch and wrongly refuse.
    p = tmp_path / "p.md"
    p.write_bytes(b"\xff")
    ctx = _ctx(tmp_path, "read", "write")
    _read_file(ctx, path="p.md")
    rec = ctx.read_state[ctx.resolve_path("p.md")]
    os.utime(p, (rec.mtime + 10, rec.mtime + 10))  # touch: mtime bumped, bytes identical
    _write_file(ctx, path="p.md", content="new")   # false-positive guard -> allowed
    assert (tmp_path / "p.md").read_text() == "new"


def test_write_refused_when_bytes_differ_but_decode_to_same_text(tmp_path: Path) -> None:
    # Proves the guard hashes RAW BYTES, not lossy decoded text: \xff and \xfe
    # both decode (errors="replace") to U+FFFD, so a decoded-text hash would
    # wrongly allow this write; a raw-bytes hash refuses it.
    p = tmp_path / "p.md"
    p.write_bytes(b"\xff")
    ctx = _ctx(tmp_path, "read", "write")
    _read_file(ctx, path="p.md")
    rec = ctx.read_state[ctx.resolve_path("p.md")]
    p.write_bytes(b"\xfe")
    os.utime(p, (rec.mtime + 10, rec.mtime + 10))
    with pytest.raises(ValueError, match="changed since you read it"):
        _write_file(ctx, path="p.md", content="new")


def test_edit_file_with_no_prior_read_still_works(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("a = 1\n")
    ctx = _ctx(tmp_path, "read", "write")
    _edit_file(ctx, path="x.py", old="a = 1", new="a = 2")
    assert (tmp_path / "x.py").read_text() == "a = 2\n"


def test_write_after_edit_without_reread_allowed(tmp_path: Path) -> None:
    (tmp_path / "x.py").write_text("a = 1\n")
    ctx = _ctx(tmp_path, "read", "write")
    _edit_file(ctx, path="x.py", old="a = 1", new="a = 2")  # refreshes the record
    _write_file(ctx, path="x.py", content="a = 3\n")        # record present -> allowed
    assert (tmp_path / "x.py").read_text() == "a = 3\n"
