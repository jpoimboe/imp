"""pin-review-base.sh prints (newline-free) the SHA that defines a review diff
range: `fork` = git merge-base <base> HEAD, `head` = git rev-parse HEAD. Runs the
real shipped script against a real git repo."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

SCRIPT = Path("src/imp/assets/workflows/imp/scripts/pin-review-base.sh").resolve()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _run(project_root: Path, mode: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "IMP_PROJECT_ROOT": str(project_root),
           "IMP_CHANGE_DIR": str(project_root / ".imp" / "change")}
    return subprocess.run(["bash", str(SCRIPT), mode], cwd=project_root.parent,
                          env=env, capture_output=True, text=True, check=False)


def _seed(project_root: Path) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    _git(project_root, "init", "-b", "main")
    _git(project_root, "config", "user.email", "t@t")
    _git(project_root, "config", "user.name", "t")
    (project_root / ".gitignore").write_text(".imp/\n")
    _git(project_root, "add", ".gitignore")
    _git(project_root, "commit", "-m", "base v1")
    _git(project_root, "checkout", "-b", "imp/x")
    (project_root / "g.txt").write_text("branch work\n")
    _git(project_root, "add", "g.txt")
    _git(project_root, "commit", "-m", "branch work")
    change_dir = project_root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main"}))


def test_fork_mode_prints_merge_base_newline_free(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr)
    expected = _git(pr, "merge-base", "main", "HEAD").stdout.strip()
    res = _run(pr, "fork")
    assert res.returncode == 0, res.stderr
    assert res.stdout == expected           # exact SHA, NO trailing newline
    assert "\n" not in res.stdout


def test_head_mode_prints_head_newline_free(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr)
    expected = _git(pr, "rev-parse", "HEAD").stdout.strip()
    res = _run(pr, "head")
    assert res.returncode == 0, res.stderr
    assert res.stdout == expected
    assert "\n" not in res.stdout


def test_unknown_mode_exits_2(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr)
    assert _run(pr, "bogus").returncode == 2


def test_fork_mode_reads_base_branch_from_change_dir(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr)
    expected = _git(pr, "merge-base", "main", "HEAD").stdout.strip()
    # Move change.json out of the project tree into a standalone change-home.
    shutil.rmtree(pr / ".imp" / "change")
    home = tmp_path / "home"
    home.mkdir()
    (home / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main"}))
    env = {**os.environ, "IMP_PROJECT_ROOT": str(pr),
           "IMP_CHANGE_DIR": str(home)}
    res = subprocess.run(["bash", str(SCRIPT), "fork"], cwd=pr.parent,
                         env=env, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert res.stdout == expected


def test_captured_sha_still_diffs_after_history_rewrite(tmp_path: Path) -> None:
    # Design guarantee the per-batch review relies on (spec #1): a captured review_base
    # SHA still diffs correctly after autosquash/amend rewrites the commit it pointed
    # at — git resolves the orphaned object regardless of reachability.
    pr = tmp_path / "wt"
    _seed(pr)
    captured = _run(pr, "head").stdout            # review_base = current HEAD
    (pr / "g.txt").write_text("amended\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "--amend", "--no-edit")     # rewrites HEAD, orphaning `captured`
    diff = _git(pr, "diff", f"{captured}..HEAD")
    assert diff.returncode == 0                    # the orphaned SHA still diffs cleanly
