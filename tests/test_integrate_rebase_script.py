from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path("src/imp/assets/workflows/imp/scripts/integrate-rebase.sh").resolve()


def _home(project_root: Path) -> Path:
    return project_root.parent / ".imp" / "quests" / "x"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _run(project_root: Path, tolerated: str = "false") -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "IMP_PROJECT_ROOT": str(project_root),
           "IMP_QUEST_DIR": str(_home(project_root))}
    return subprocess.run(["bash", str(SCRIPT), tolerated], cwd=project_root.parent,
                          env=env, capture_output=True, text=True, check=False)


def _stamp(project_root: Path, name: str) -> None:
    """Write HEAD's tree SHA into a run-verify stamp file under the change dir."""
    tree = _git(project_root, "rev-parse", "HEAD^{tree}").stdout.strip()
    cd = _home(project_root)
    cd.mkdir(parents=True, exist_ok=True)
    (cd / name).write_text(tree + "\n")


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
    fork = _git(project_root, "merge-base", "main", "HEAD").stdout.strip()
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
    (cd / "quest.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main", "cur_base_head": fork}))


def test_base_unmoved_cleared_by_green_stamp_exits_2(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    _stamp(pr, "last_green_tree_slow")          # HEAD tree verified green
    r = _run(pr)
    assert r.returncode == 2                    # cleared -> merge directly


def test_bad_base_ref_exits_3(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    cd = _home(pr)
    (cd / "quest.json").write_text(
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
    assert r.returncode == 6                                  # conflict -> reconciler
    # Rebase must be left in-progress for the reconciler to resolve
    rebase_active = (pr / ".git" / "rebase-merge").exists() or \
                    (pr / ".git" / "rebase-apply").exists()
    assert rebase_active, "rebase must be left in-progress for the reconciler"


def test_clean_rebase_prints_fork_sha_and_replants_cur_base_head(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    before = json.loads((_home(pr) / "quest.json").read_text())["cur_base_head"]
    r = _run(pr)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == before
    b = _git(pr, "rev-parse", "main").stdout.strip()
    meta = json.loads((_home(pr) / "quest.json").read_text())
    assert meta["cur_base_head"] == b                           # cur_base_head := B
    assert _git(pr, "diff", "--quiet", before, b).returncode == 1   # (P,B) non-empty, recoverable
    mb = _git(pr, "merge-base", "main", "HEAD").stdout.strip()
    assert _git(pr, "diff", "--quiet", mb, "main").returncode == 0  # empty post-rebase


def test_conflict_prints_fork_sha_and_keeps_cur_base_head(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=True)
    before = json.loads((_home(pr) / "quest.json").read_text())["cur_base_head"]
    r = _run(pr)
    assert r.returncode == 6, r.stderr
    assert r.stdout.strip() == before
    after = json.loads((_home(pr) / "quest.json").read_text())["cur_base_head"]
    assert after == before                                      # untouched on exit 6


def test_base_unmoved_cleared_keeps_cur_base_head_and_prints_no_fork(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    _stamp(pr, "last_green_tree_slow")
    before = json.loads((_home(pr) / "quest.json").read_text())["cur_base_head"]
    r = _run(pr)
    assert r.returncode == 2
    after = json.loads((_home(pr) / "quest.json").read_text())["cur_base_head"]
    assert after == before                                      # untouched on exit 2
    assert r.stdout.strip() == ""                               # no fork SHA on the fast path


def test_base_unmoved_uncleared_exits_7_and_prints_fork(tmp_path: Path) -> None:
    """Base unmoved, no stamp, not tolerated -> exit 7, prints fork SHA, cur_base_head untouched."""
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    before = json.loads((_home(pr) / "quest.json").read_text())["cur_base_head"]
    r = _run(pr)                                # tolerated defaults false, no stamp
    assert r.returncode == 7, r.stderr
    assert r.stdout.strip() == before
    after = json.loads((_home(pr) / "quest.json").read_text())["cur_base_head"]
    assert after == before


def test_base_unmoved_tolerated_red_on_this_tree_exits_2(tmp_path: Path) -> None:
    """Tolerated red whose last_verified_tree_slow matches HEAD's tree -> cleared, exit 2."""
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    _stamp(pr, "last_verified_tree_slow")       # slow ran on THIS tree (red, tolerated)
    r = _run(pr, tolerated="true")
    assert r.returncode == 2, r.stderr


def test_base_unmoved_tolerated_but_stale_verified_tree_exits_7(tmp_path: Path) -> None:
    """Toleration set but last_verified_tree_slow is a different tree -> exit 7."""
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    cd = _home(pr)
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "last_verified_tree_slow").write_text("deadbeef\n")   # some other tree
    r = _run(pr, tolerated="true")
    assert r.returncode == 7, r.stderr


def test_base_unmoved_tolerated_flag_normalized_from_capitalized_bool(tmp_path: Path) -> None:
    """Capitalized 'True' is lowercased by the script so toleration is recognized."""
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    _stamp(pr, "last_verified_tree_slow")
    r = _run(pr, tolerated="True")
    assert r.returncode == 2, r.stderr
