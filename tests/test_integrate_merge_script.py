"""integrate-merge.sh: merges base into the change branch without committing,
classifying via exit code. Runs the real shipped script against a real git repo."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path("src/imp/assets/workflows/default/scripts/integrate-merge.sh").resolve()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _run(project_root: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "IMP_PROJECT_ROOT": str(project_root)}
    # cwd is deliberately outside IMP_PROJECT_ROOT — the script cd's there itself.
    return subprocess.run(["bash", str(SCRIPT)], cwd=project_root.parent,
                          env=env, capture_output=True, text=True, check=False)


def _seed(project_root: Path, *, base_ahead: bool, conflict: bool) -> None:
    """Repo on branch imp/x; base 'main' is `base_ahead`/conflicting as requested.

    conflict only takes effect when base_ahead=True.
    """
    project_root.mkdir(parents=True, exist_ok=True)
    _git(project_root, "init", "-b", "main")
    _git(project_root, "config", "user.email", "t@t")
    _git(project_root, "config", "user.name", "t")
    # MANDATORY (review finding #1): commit a `.gitignore` covering `.imp/` BEFORE
    # anything writes under .imp/. The change.json this helper writes — plus the
    # script's own integrated_base_sha / reintegration_count writes (Task 9) — are
    # untracked; without this they'd surface in `git status` and other tests'
    # clean-porcelain assumptions break. Mirrors production (worktree.py requires
    # .imp/ git-ignored via committed .gitignore / info/exclude).
    (project_root / ".gitignore").write_text(".imp/\n")
    _git(project_root, "add", ".gitignore")
    _git(project_root, "commit", "-m", "gitignore .imp/")
    (project_root / "f.txt").write_text("base v1\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v1")
    _git(project_root, "checkout", "-b", "imp/x")
    (project_root / "g.txt").write_text("branch work\n")
    _git(project_root, "add", "g.txt")
    _git(project_root, "commit", "-m", "branch work")
    if base_ahead:
        _git(project_root, "checkout", "main")
        # On conflict, base and branch both diverge from v1 on the SAME file so the
        # merge collides; otherwise base edits a line the branch leaves alone.
        line = "base v2 (conflicting edit)\n" if conflict else "base v2\n"
        (project_root / "f.txt").write_text(line)
        _git(project_root, "add", "f.txt")
        _git(project_root, "commit", "-m", "base v2")
        _git(project_root, "checkout", "imp/x")
        if conflict:
            (project_root / "f.txt").write_text("branch-side conflicting edit\n")
            _git(project_root, "add", "f.txt")
            _git(project_root, "commit", "-m", "branch edits f")
    change_dir = project_root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main"}))


def test_nothing_to_integrate_exits_2(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    assert _run(pr).returncode == 2


def test_clean_integration_exits_0_and_stages(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()
    assert _run(pr).returncode == 0
    # base v2 content present...
    assert (pr / "f.txt").read_text() == "base v2\n"
    # ...AND the merge is in progress, NOT committed (the uncommitted-handoff
    # contract the integrator depends on): MERGE_HEAD exists and HEAD is unmoved.
    assert _git(pr, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before


def test_conflict_exits_0_leaves_conflicted_tree(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=True)
    assert _run(pr).returncode == 0
    # Conflict markers present for the integrator to resolve.
    assert "<<<<<<<" in (pr / "f.txt").read_text()
    assert _git(pr, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0


def test_tracked_dirty_entry_tree_exits_5(tmp_path: Path) -> None:
    # The dirty-entry guard prevents pre-existing TRACKED dirt being folded in.
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    (pr / "g.txt").write_text("uncommitted tracked edit\n")   # g.txt is tracked
    assert _run(pr).returncode == 5
    # Refused before starting any merge.
    assert _git(pr, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode != 0


def test_untracked_scratch_does_not_trip_entry_guard(tmp_path: Path) -> None:
    # Finding #9: untracked scratch files must NOT trip the guard — integration
    # proceeds (they're never folded into the merge commit).
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    (pr / "scratch.tmp").write_text("agent scratch, untracked\n")
    assert _run(pr).returncode == 0
    assert _git(pr, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0


def test_bad_base_ref_exits_3(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    meta = pr / ".imp" / "change" / "change.json"
    meta.write_text(json.dumps({"id": "x", "title": "t", "base_branch": "nope"}))
    assert _run(pr).returncode == 3


def test_empty_base_ref_exits_5(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    meta = pr / ".imp" / "change" / "change.json"
    meta.write_text(json.dumps({"id": "x", "title": "t", "base_branch": ""}))
    assert _run(pr).returncode == 5


def test_stale_merge_head_at_entry_exits_5(tmp_path: Path) -> None:
    # A leftover in-progress merge (MERGE_HEAD present, clean index) must pause,
    # not be misread as a freshly staged merge.
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    # Start a merge and leave it in progress (do not commit/abort).
    _git(pr, "merge", "--no-ff", "--no-commit", "main")
    # Sanity: MERGE_HEAD is present now.
    assert _git(pr, "rev-parse", "-q", "--verify", "MERGE_HEAD").returncode == 0
    assert _run(pr).returncode == 5
