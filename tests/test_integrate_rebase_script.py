from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path("src/imp/assets/workflows/imp/scripts/integrate-rebase.sh").resolve()


def _home(project_root: Path) -> Path:
    return project_root.parent / ".imp" / "changes" / "x"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _run(project_root: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "IMP_PROJECT_ROOT": str(project_root),
           "IMP_CHANGE_DIR": str(_home(project_root))}
    return subprocess.run(["bash", str(SCRIPT)], cwd=project_root.parent,
                          env=env, capture_output=True, text=True, check=False)


def _seed(project_root: Path, *, base_ahead: bool, conflict: bool) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    _git(project_root, "init", "-b", "main")
    _git(project_root, "config", "user.email", "t@t")
    _git(project_root, "config", "user.name", "t")
    (project_root / ".gitignore").write_text(".imp/\n")
    _git(project_root, "add", ".gitignore")
    _git(project_root, "commit", "-m", "gitignore")
    (project_root / "f.txt").write_text("base v1\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v1")
    _git(project_root, "checkout", "-b", "imp/x")
    (project_root / "g.txt").write_text("branch work\n")
    _git(project_root, "add", "g.txt")
    _git(project_root, "commit", "-m", "branch work")
    if base_ahead:
        _git(project_root, "checkout", "main")
        (project_root / "f.txt").write_text(
            "base v2 (conflicting)\n" if conflict else "base v2\n")
        _git(project_root, "add", "f.txt")
        _git(project_root, "commit", "-m", "base v2")
        _git(project_root, "checkout", "imp/x")
        if conflict:
            (project_root / "f.txt").write_text("branch-side conflicting\n")
            _git(project_root, "add", "f.txt")
            _git(project_root, "commit", "-m", "branch edits f")
    cd = _home(project_root)
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "change.json").write_text(json.dumps({"id": "x", "title": "t", "base_branch": "main"}))


def test_base_unmoved_exits_2(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    r = _run(pr)
    assert r.returncode == 2    # empty base-side diff -> merge directly


def test_bad_base_ref_exits_3(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    cd = _home(pr)
    (cd / "change.json").write_text(
        json.dumps({"id": "x", "title": "t", "base_branch": "nonexistent"}))
    r = _run(pr)
    assert r.returncode == 3


def test_leftover_rebase_exits_5(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    # Simulate a leftover rebase-merge directory from a prior interrupted run
    rebase_dir = pr / ".git" / "rebase-merge"
    rebase_dir.mkdir()
    r = _run(pr)
    assert r.returncode == 5


def test_dirty_tracked_tree_exits_5(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    # Dirty a tracked file without staging it
    (pr / "g.txt").write_text("modified but not committed\n")
    r = _run(pr)
    assert r.returncode == 5


def test_base_moved_clean_exits_0(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    r = _run(pr)
    assert r.returncode == 0                                  # clean rebase -> integration-review
    assert _git(pr, "merge-base", "--is-ancestor", "main", "HEAD").returncode == 0  # linear on base


def test_base_moved_conflict_exits_6(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=True)
    r = _run(pr)
    assert r.returncode == 6                                  # conflict -> integrator
    # Rebase must be left in-progress for the integrator to resolve
    rebase_active = (pr / ".git" / "rebase-merge").exists() or \
                    (pr / ".git" / "rebase-apply").exists()
    assert rebase_active, "rebase must be left in-progress for the integrator"
