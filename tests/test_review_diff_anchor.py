from __future__ import annotations

import os
import subprocess
from pathlib import Path


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=False,
                          env={**os.environ, "GIT_EDITOR": "true"})


def _init(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "base.txt")
    _git(repo, "commit", "-m", "base")


def _names(cp: subprocess.CompletedProcess[str]) -> list[str]:
    return [s for s in cp.stdout.splitlines() if s]


def test_change_branch_diff_is_cwd_independent_and_change_scoped(tmp_path: Path) -> None:
    repo = tmp_path / "wt"
    _init(repo)
    fork = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # The change branch forks at `fork` and adds one file.
    _git(repo, "checkout", "-b", "imp/x")
    (repo / "change.txt").write_text("change\n")
    _git(repo, "add", "change.txt")
    _git(repo, "commit", "-m", "the change")

    # Base advances past the fork on main with an unrelated file.
    _git(repo, "checkout", "main")
    (repo / "advanced.txt").write_text("advanced\n")
    _git(repo, "add", "advanced.txt")
    _git(repo, "commit", "-m", "advance base")

    # A second checkout of the change branch (agent wandered out of place).
    other = tmp_path / "elsewhere"
    _git(repo, "worktree", "add", str(other), "imp/x")

    # The branch-anchored form yields only the change's file from EITHER cwd.
    for cwd in (repo, other):
        diff = _names(_git(cwd, "diff", "--name-only", f"{fork}..imp/x"))
        assert diff == ["change.txt"]

    # The old ambient-HEAD form, run from the advanced main checkout, leaks the
    # base's unrelated commits and misses the change entirely.
    leaked = _names(_git(repo, "diff", "--name-only", f"{fork}..HEAD"))
    assert "advanced.txt" in leaked
    assert "change.txt" not in leaked
