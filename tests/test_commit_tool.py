"""The commit tool (spec §12): stages paths, commits with a Change: <slug>
trailer, idempotent on amend. Not fs-permission gated."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from imp.agents.tools.commit import commit
from imp.agents.tools.context import ToolContext
from tests.conftest import _init_repo


def _ctx(root: Path, slug: str) -> ToolContext:
    ctx = ToolContext(project_root=root, fs_permissions=(),
                      events_path=root / "events.jsonl")
    ctx.change_context = SimpleNamespace(slug=slug, title="t",
                                         project_root=root, workflow_dir=root)
    return ctx


def _body(root: Path) -> str:
    return subprocess.run(
        ["git", "log", "-1", "--format=%B"], cwd=root,
        capture_output=True, text=True, check=True,
    ).stdout


def _count_commits(root: Path) -> int:
    out = subprocess.run(["git", "rev-list", "--count", "HEAD"], cwd=root,
                         capture_output=True, text=True, check=True).stdout
    return int(out.strip())


def test_commit_stamps_change_trailer(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("hi\n")
    ctx = _ctx(tmp_path, "2026-06-01-demo")
    msg = commit(ctx, paths=["/project/f.txt"], message="feat: add f")
    body = _body(tmp_path)
    assert "feat: add f" in body
    assert "Change: 2026-06-01-demo" in body
    assert "committed" in msg


def test_commit_amend_does_not_duplicate_trailer(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("a\n")
    ctx = _ctx(tmp_path, "2026-06-01-demo")
    commit(ctx, paths=["/project/f.txt"], message="feat: x")
    before = _count_commits(tmp_path)
    (tmp_path / "f.txt").write_text("b\n")
    commit(ctx, paths=["/project/f.txt"], message="feat: x (fixed)", amend=True)
    body = _body(tmp_path)
    assert body.count("Change: 2026-06-01-demo") == 1  # idempotent
    assert "feat: x (fixed)" in body
    assert _count_commits(tmp_path) == before  # amended, not a new commit


def test_commit_rejects_path_outside_project(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ctx = _ctx(tmp_path, "2026-06-01-demo")
    with pytest.raises(ValueError, match="outside /project"):
        commit(ctx, paths=["/etc/passwd"], message="nope")


def test_commit_surfaces_nothing_to_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ctx = _ctx(tmp_path, "2026-06-01-demo")
    # No staged changes (the seed README is already committed by _init_repo).
    with pytest.raises(ValueError, match="git commit failed"):
        commit(ctx, paths=[], message="empty")
