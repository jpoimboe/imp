"""PermissionFilteringBackend: delegate when allowed, raise when denied."""

from __future__ import annotations

from pathlib import Path

import pytest
from deepagents.backends import FilesystemBackend  # type: ignore[import-untyped]

from imp.agents.permission_backend import PermissionFilteringBackend
from imp.agents.permissions import PermissionDenied
from imp.engine.types import FsRule, SourceLoc


def _rule(ops: tuple[str, ...], paths: tuple[str, ...], mode: str) -> FsRule:
    return FsRule(
        operations=ops, paths=paths, mode=mode,  # type: ignore[arg-type]
        source=SourceLoc(file=Path("x"), line=1),
    )


def test_allowed_read_passes_through(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hi")
    inner = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    rules = (_rule(("read",), ("/**",), "allow"),)
    wrapped = PermissionFilteringBackend(inner, rules, prefix="/")
    result = wrapped.read("/a.txt")
    # FilesystemBackend.read returns ReadResult; content lives in file_data.
    assert result.file_data["content"] == "hi"


def test_denied_write_raises(tmp_path: Path) -> None:
    inner = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    rules = (_rule(("write",), ("/**",), "deny"),)
    wrapped = PermissionFilteringBackend(inner, rules, prefix="/")
    with pytest.raises(PermissionDenied):
        wrapped.write("/a.txt", "x")


def test_prefix_aware(tmp_path: Path) -> None:
    """When the inner backend is mounted at /project/, paths into it look
    like /a.txt at the backend level but should be checked as /project/a.txt
    against the rules."""
    inner = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    rules = (
        _rule(("write",), ("/project/scratch/**",), "allow"),
        _rule(("write",), ("/project/**",), "deny"),
    )
    wrapped = PermissionFilteringBackend(inner, rules, prefix="/project/")
    (tmp_path / "scratch").mkdir()
    wrapped.write("/scratch/ok.txt", "yes")
    with pytest.raises(PermissionDenied):
        wrapped.write("/other.txt", "no")


def test_unaffected_operations_pass(tmp_path: Path) -> None:
    """ls, glob, grep don't take a check_permission op — they should still
    pass through. Only read/write are gated in V0."""
    (tmp_path / "a.txt").write_text("hi")
    inner = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    rules = (_rule(("write",), ("/**",), "deny"),)
    wrapped = PermissionFilteringBackend(inner, rules, prefix="/")
    listing = wrapped.ls("/")
    assert listing  # got something


def test_traversal_normalized_before_rule_check(tmp_path: Path) -> None:
    """A path with .. is normalized before evaluating against rules."""
    inner = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    rules = (
        _rule(("write",), ("/project/scratch/**",), "allow"),
        _rule(("write",), ("/project/**",), "deny"),
    )
    wrapped = PermissionFilteringBackend(inner, rules, prefix="/project/")
    # /scratch/../other.txt normalizes to /other.txt → matches the deny rule
    with pytest.raises(PermissionDenied):
        wrapped.write("/scratch/../other.txt", "nope")


def test_denied_edit_raises(tmp_path: Path) -> None:
    """edit() uses the write permission — denied paths raise."""
    inner = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    rules = (_rule(("write",), ("/**",), "deny"),)
    wrapped = PermissionFilteringBackend(inner, rules, prefix="/")
    with pytest.raises(PermissionDenied):
        wrapped.edit("/a.txt", "old", "new")


# --- Bug K: *args/**kwargs forwarding ---


def test_read_forwards_kwargs(tmp_path: Path) -> None:
    """deepagents' read_file tool may pass offset/limit; the wrapper must
    forward them so partial reads work."""
    (tmp_path / "big.txt").write_text("line1\nline2\nline3\nline4\n")
    inner = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    rules = (_rule(("read",), ("/**",), "allow"),)
    wrapped = PermissionFilteringBackend(inner, rules, prefix="/")
    # Behavior depends on what FilesystemBackend.read accepts; at minimum,
    # the call must not raise TypeError on unexpected kwargs.
    try:
        result = wrapped.read("/big.txt", offset=0, limit=2)
        # If it didn't raise, the kwargs got forwarded
        assert result is not None
    except TypeError as e:
        # If FilesystemBackend itself rejected the kwarg, that's its problem —
        # but the wrapper must not be the source of TypeError
        msg = str(e)
        assert "PermissionFilteringBackend.read" not in msg, (
            f"wrapper swallowed kwargs: {msg}"
        )


def test_write_forwards_kwargs(tmp_path: Path) -> None:
    inner = FilesystemBackend(root_dir=str(tmp_path), virtual_mode=True)
    rules = (_rule(("write",), ("/**",), "allow"),)
    wrapped = PermissionFilteringBackend(inner, rules, prefix="/")
    try:
        wrapped.write("/a.txt", content="hi")  # kwarg form
    except TypeError as e:
        assert "PermissionFilteringBackend.write" not in str(e)
