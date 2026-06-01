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


def test_commit_fixup_targets_a_buried_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ctx = _ctx(tmp_path, "2026-06-01-demo")
    (tmp_path / "f.txt").write_text("a\n")
    commit(ctx, paths=["/project/f.txt"], message="feat: target task")
    target = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    # Bury it under another commit so amend is not an option.
    (tmp_path / "g.txt").write_text("x\n")
    commit(ctx, paths=["/project/g.txt"], message="feat: later task")
    # Fix the first task via fixup.
    (tmp_path / "f.txt").write_text("a-fixed\n")
    msg = commit(ctx, paths=["/project/f.txt"], message="", fixup=target)
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert subject.startswith("fixup! feat: target task")
    assert "fixup" in msg


def test_commit_fixup_and_amend_are_mutually_exclusive(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ctx = _ctx(tmp_path, "2026-06-01-demo")
    with pytest.raises(ValueError, match="amend.*fixup|fixup.*amend"):
        commit(ctx, paths=[], message="", amend=True, fixup="HEAD")


def test_commit_fixup_tag_anchor_has_no_prefix_collision(tmp_path: Path) -> None:
    # MAJOR-1 regression: `:/imp-task-1-end` must target task 1, NOT the
    # more-recently-committed task 10 (whose body contains the substring
    # `imp-task-1`). The `-end` delimiter prevents the prefix collision.
    _init_repo(tmp_path)
    ctx = _ctx(tmp_path, "2026-06-01-demo")
    (tmp_path / "a.txt").write_text("1\n")
    commit(ctx, paths=["/project/a.txt"], message="feat: task one\n\nTask: imp-task-1-end")
    (tmp_path / "b.txt").write_text("10\n")              # committed AFTER (most recent)
    commit(ctx, paths=["/project/b.txt"], message="feat: task ten\n\nTask: imp-task-10-end")
    (tmp_path / "a.txt").write_text("1-fixed\n")
    commit(ctx, paths=["/project/a.txt"], message="", fixup=":/imp-task-1-end")
    subject = subprocess.run(
        ["git", "log", "-1", "--format=%s"], cwd=tmp_path,
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert subject == "fixup! feat: task one"            # NOT "...task ten"


def test_commit_fixup_requires_paths(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    ctx = _ctx(tmp_path, "2026-06-01-demo")
    with pytest.raises(ValueError, match="fixup requires at least one path"):
        commit(ctx, paths=[], message="", fixup="HEAD")
