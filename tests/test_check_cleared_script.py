"""check-cleared.sh — the merge door's clearance gate between integration-reviewer
and summarizer. Same subprocess harness shape as tests/test_assert_stamped_script.py:
a real throwaway git repo plus a quest home holding run-verify.sh's stamp files."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from imp.paths import project_state_dir

CHECK = Path(
    "src/imp/assets/workflows/default/scripts/check-cleared.sh").resolve()


def _home(pr: Path) -> Path:
    return project_state_dir(pr.parent) / "quests" / "x"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=False, env={**os.environ, "GIT_EDITOR": "true"})


def _init(pr: Path) -> None:
    pr.mkdir(parents=True, exist_ok=True)
    _git(pr, "init", "-b", "main")
    _git(pr, "config", "user.email", "t@t")
    _git(pr, "config", "user.name", "t")
    (pr / ".gitignore").write_text(".imp/\n.imp-isolated/\n")
    _git(pr, "add", ".gitignore")
    _git(pr, "commit", "-m", "gitignore .imp/")
    _home(pr).mkdir(parents=True, exist_ok=True)


def _stamp(pr: Path, name: str, tree: str | None = None) -> None:
    """Write a run-verify stamp: HEAD's tree by default, or an explicit other tree."""
    if tree is None:
        tree = _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip()
    (_home(pr) / name).write_text(tree + "\n")


def _run(pr: Path, tolerated: str = "false") -> subprocess.CompletedProcess[str]:
    # Exec'd directly rather than via `bash <path>` so the shipped file's +x bit
    # is exercised — the engine invokes it as ./scripts/check-cleared.sh.
    return subprocess.run(
        [str(CHECK), tolerated], cwd=pr.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(pr),
             "IMP_QUEST_DIR": str(_home(pr))},
        capture_output=True, text=True, check=False)


def test_green_stamp_on_head_tree_clears(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _stamp(pr, "last_green_tree_slow")
    res = _run(pr)
    assert res.returncode == 0, res.stderr


def test_tolerated_red_on_head_tree_clears_with_the_capitalized_flag(
    tmp_path: Path,
) -> None:
    # The workflow renders a bool var capitalized; the script must lowercase it.
    pr = tmp_path / "wt"
    _init(pr)
    _stamp(pr, "last_verified_tree_slow")
    res = _run(pr, tolerated="True")
    assert res.returncode == 0, res.stderr


def test_tolerated_red_without_the_flag_is_not_cleared(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _stamp(pr, "last_verified_tree_slow")
    res = _run(pr)
    assert res.returncode == 3, res.stdout + res.stderr


def test_no_stamps_routes_back_to_the_gate(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    res = _run(pr)
    assert res.returncode == 3, res.stdout + res.stderr
    # The workflow recovers by itself; no copy tells the human to drive it.
    assert "imp resume" not in res.stdout + res.stderr


def test_stamp_for_a_different_tree_is_not_cleared(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _stamp(pr, "last_green_tree_slow", tree="deadbeef")
    _stamp(pr, "last_verified_tree_slow", tree="deadbeef")
    res = _run(pr, tolerated="True")
    assert res.returncode == 3, res.stdout + res.stderr
