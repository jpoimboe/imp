"""integrate-rebase.sh: rebases the change branch ONTO the base tip (linear, no
merge commit), classifying via exit code. Runs the real shipped script against a
real git repo."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

SCRIPT = Path("src/imp/assets/workflows/imp/scripts/integrate-rebase.sh").resolve()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _run(project_root: Path) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "IMP_PROJECT_ROOT": str(project_root)}
    # cwd is deliberately outside IMP_PROJECT_ROOT — the script cd's there itself.
    return subprocess.run(["bash", str(SCRIPT)], cwd=project_root.parent,
                          env=env, capture_output=True, text=True, check=False)


def _rebase_in_progress(pr: Path) -> bool:
    d = _git(pr, "rev-parse", "--git-path", "rebase-merge").stdout.strip()
    a = _git(pr, "rev-parse", "--git-path", "rebase-apply").stdout.strip()
    return (pr / d).is_dir() or (pr / a).is_dir()


def _seed(project_root: Path, *, base_ahead: bool, conflict: bool) -> None:
    """Repo on branch imp/x; base 'main' is `base_ahead`/conflicting as requested.

    conflict only takes effect when base_ahead=True.
    """
    project_root.mkdir(parents=True, exist_ok=True)
    _git(project_root, "init", "-b", "main")
    _git(project_root, "config", "user.email", "t@t")
    _git(project_root, "config", "user.name", "t")
    # git-ignore .imp/ BEFORE anything writes there (mirrors production; otherwise
    # the change.json / integrated_base_sha / reintegration_count writes surface as
    # untracked dirt that other tests' clean-porcelain assumptions break on).
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


def test_clean_rebase_exits_0_and_linearizes(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    assert _run(pr).returncode == 0
    # base content present, base is now an ancestor (linear), no merge, no rebase left.
    assert (pr / "f.txt").read_text() == "base v2\n"
    assert _git(pr, "merge-base", "--is-ancestor", "main", "HEAD").returncode == 0
    assert _git(pr, "rev-list", "--merges", "main..HEAD").stdout.strip() == ""
    assert not _rebase_in_progress(pr)


def test_conflict_rebase_exits_6_leaves_rebase_in_progress(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=True)
    assert _run(pr).returncode == 6
    assert "<<<<<<<" in (pr / "f.txt").read_text()
    assert _rebase_in_progress(pr)


def test_leftover_rebase_at_entry_exits_5(tmp_path: Path) -> None:
    # The first run leaves a rebase in progress (exit 6); a second entry must see
    # the leftover rebase and pause, not re-classify.
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=True)
    assert _run(pr).returncode == 6
    assert _run(pr).returncode == 5


def test_tracked_dirty_entry_tree_exits_5(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    (pr / "g.txt").write_text("uncommitted tracked edit\n")   # g.txt is tracked
    assert _run(pr).returncode == 5
    assert not _rebase_in_progress(pr)                        # refused before any rebase


def test_untracked_scratch_does_not_trip_entry_guard(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    (pr / "scratch.tmp").write_text("agent scratch, untracked\n")
    assert _run(pr).returncode == 0


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


def test_records_integrated_base_sha_on_clean_rebase(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    assert _run(pr).returncode == 0
    recorded = (pr / ".imp" / "change" / "integrated_base_sha").read_text().strip()
    assert recorded == _git(pr, "rev-parse", "main^{commit}").stdout.strip()


def test_records_sha_even_when_nothing_to_integrate(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    assert _run(pr).returncode == 2
    recorded = (pr / ".imp" / "change" / "integrated_base_sha").read_text().strip()
    assert recorded == _git(pr, "rev-parse", "main^{commit}").stdout.strip()


def test_records_integrated_base_sha_on_conflict_rebase(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=True)
    assert _run(pr).returncode == 6
    recorded = (pr / ".imp" / "change" / "integrated_base_sha").read_text().strip()
    assert recorded == _git(pr, "rev-parse", "main^{commit}").stdout.strip()


def test_noop_path_deletes_stale_integration_notes(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=False, conflict=False)
    stale = pr / ".imp" / "change" / "integration-notes.md"
    stale.write_text("stale notes from prior conflict round\n")
    assert _run(pr).returncode == 2
    assert not stale.exists()


def test_clean_rebase_path_deletes_stale_integration_notes(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    stale = pr / ".imp" / "change" / "integration-notes.md"
    stale.write_text("stale notes from prior conflict round\n")
    assert _run(pr).returncode == 0
    assert not stale.exists()


def test_nonnumeric_reintegration_limit_exits_4(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    env = {**os.environ, "IMP_PROJECT_ROOT": str(pr), "IMP_MAX_REINTEGRATIONS": "abc"}
    rc = subprocess.run(["bash", str(SCRIPT)], cwd=pr.parent, env=env,
                        capture_output=True, text=True, check=False).returncode
    assert rc == 4


def test_reintegration_counter_pauses_after_limit(tmp_path: Path) -> None:
    # Each round: the clean rebase replays the branch onto the current base tip and
    # bumps the counter; then base advances with a fresh commit so the next round
    # actually integrates. At the limit the script pauses WITHOUT bumping.
    pr = tmp_path / "wt"
    _seed(pr, base_ahead=True, conflict=False)
    env = {**os.environ, "IMP_PROJECT_ROOT": str(pr), "IMP_MAX_REINTEGRATIONS": "2"}

    def run() -> int:
        return subprocess.run(
            ["bash", str(SCRIPT)], cwd=pr.parent, env=env,
            capture_output=True, text=True, check=False,
        ).returncode

    advance_count = [0]

    def advance_base() -> None:
        advance_count[0] += 1
        _git(pr, "checkout", "main")
        fname = f"more_{advance_count[0]}.txt"
        (pr / fname).write_text(f"more base {advance_count[0]}\n")
        _git(pr, "add", fname)
        _git(pr, "commit", "-m", "base advances")
        _git(pr, "checkout", "imp/x")

    r1 = run()      # PREV=0 < 2 -> bump to 1, clean rebase onto v2
    advance_base()  # base advances to v3
    r2 = run()      # PREV=1 < 2 -> bump to 2, clean rebase onto v3
    advance_base()  # base advances to v4
    r3 = run()      # PREV=2 >= 2 -> exit 4 (no bump, no record)
    assert (r1, r2, r3) == (0, 0, 4)
    # The exit-4 pause must NOT record the un-integrated current base tip — the
    # recorded SHA still points at the last base actually integrated (v3).
    recorded = (pr / ".imp" / "change" / "integrated_base_sha").read_text().strip()
    assert recorded != _git(pr, "rev-parse", "main^{commit}").stdout.strip()
