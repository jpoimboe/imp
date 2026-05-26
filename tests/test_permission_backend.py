"""PermissionFilteringBackend: delegate when allowed, raise when denied."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.permission_backend import PermissionFilteringBackend
from imp.agents.permissions import PermissionDenied
from imp.engine.types import FsRule, SourceLoc

from deepagents.backends import FilesystemBackend  # type: ignore[import-untyped]


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
