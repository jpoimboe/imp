from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from imp.cli.init import BaseDetection, _resolve_init_base


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "t")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "t@t")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "t")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "t@t")


def _init_repo(root: Path, branch: str) -> None:
    subprocess.run(["git", "init", "-q", "-b", branch, str(root)], check=True)


def _commit(root: Path) -> None:
    subprocess.run(
        ["git", "-C", str(root), "commit", "--allow-empty", "-qm", "seed"],
        check=True,
    )


def test_resolve_prefers_origin_head(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "feature")
    subprocess.run(
        ["git", "-C", str(root), "symbolic-ref",
         "refs/remotes/origin/HEAD", "refs/remotes/origin/mainline"],
        check=True,
    )
    assert _resolve_init_base(root, git_created=False) == BaseDetection(
        "mainline", "from origin/HEAD")


def test_resolve_unborn_repo_is_a_new_repo(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "trunk")  # unborn: no commits yet
    assert _resolve_init_base(root, git_created=False) == BaseDetection(
        "trunk", "new repo")


def test_resolve_git_created_this_run_is_a_new_repo(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "main")
    _commit(root)  # has commits, but git_created still means a new repo
    assert _resolve_init_base(root, git_created=True) == BaseDetection(
        "main", "new repo")


def test_resolve_existing_repo_adopts_the_current_branch(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "main")
    _commit(root)
    # No origin/HEAD, born, not created this run: the current branch, no prompt.
    assert _resolve_init_base(root, git_created=False) == BaseDetection(
        "main", "current branch")


def test_resolve_detached_head_yields_no_branch(tmp_path: Path) -> None:
    root = tmp_path / "r"
    _init_repo(root, "main")
    _commit(root)
    subprocess.run(
        ["git", "-C", str(root), "checkout", "--detach", "-q"], check=True)
    assert _resolve_init_base(root, git_created=False) is None
