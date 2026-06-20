"""autosquash.sh folds fixup!/squash!/amend! commits into their targets.

The branch is always LINEAR here (base integration rebases, never merges), so this
is the plain fork-point autosquash with no merge-recreation machinery. Exercised
against the real shipped script + real git:

- No fixups in range: fast-path no-op (exit 0).
- Ordinary fork-point autosquash folds fixup! / squash! directives.
- A fold that conflicts on reorder is handed to autosquash-resolve (exit 2): the
  rebase is left in progress and the pre-autosquash HEAD + tree are persisted.
- A non-conflict rebase failure is a hard failure (exit 1), no oracle written."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

AUTOSQUASH = Path("src/imp/assets/workflows/imp/scripts/autosquash.sh").resolve()


def _home(project_root: Path) -> Path:
    return project_root.parent / ".imp" / "changes" / "x"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    # GIT_EDITOR=true: setup commits like `commit --squash=<sha>` (no -m) open
    # an interactive editor. Without this pin the test is environment-dependent:
    # genuine on hosts that already export GIT_EDITOR=true, VACUOUS on hosts
    # where the editor fails fast (the squash commit silently never lands), and
    # a hard WEDGE in editor-less sandboxes — it hung the dogfood's run-tests
    # gate under bwrap --clearenv (2026-06-09).
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True,
                          text=True, check=False,
                          env={**os.environ, "GIT_EDITOR": "true"})


def _run(project_root: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(AUTOSQUASH)],
        cwd=project_root.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(project_root),
             "IMP_CHANGE_DIR": str(_home(project_root))},
        capture_output=True, text=True, check=False,
    )


def _init(project_root: Path) -> None:
    project_root.mkdir(parents=True, exist_ok=True)
    _git(project_root, "init", "-b", "main")
    _git(project_root, "config", "user.email", "t@t")
    _git(project_root, "config", "user.name", "t")
    # MANDATORY: git-ignore .imp/ BEFORE anything writes there (mirrors production;
    # otherwise .imp/ surfaces as untracked dirt that breaks porcelain assumptions
    # in other tests sharing the filesystem).
    (project_root / ".gitignore").write_text(".imp/\n")
    _git(project_root, "add", ".gitignore")
    _git(project_root, "commit", "-m", "gitignore .imp/")


def _record_change(project_root: Path) -> None:
    change_dir = _home(project_root)
    change_dir.mkdir(parents=True, exist_ok=True)
    fork = _git(project_root, "rev-parse", "main").stdout.strip()
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main", "cur_base_head": fork}))


def _subjects(pr: Path) -> list[str]:
    fork = _git(pr, "merge-base", "main", "HEAD").stdout.strip()
    out = _git(pr, "log", "--format=%s", f"{fork}..HEAD").stdout
    return [s for s in out.splitlines() if s]


def test_autosquash_folds_with_no_integration_merge(tmp_path: Path) -> None:
    """No merge in range: the ordinary fork-point autosquash still folds fixups."""
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "branch work")
    target = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "g.txt").write_text("work (fixed)\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", f"--fixup={target}")

    result = _run(pr)
    assert result.returncode == 0, result.stderr
    assert not any(s.startswith("fixup!") for s in _subjects(pr))
    assert (pr / "g.txt").read_text() == "work (fixed)\n"


def test_autosquash_folds_squash_only_no_fixup(tmp_path: Path) -> None:
    """A branch reworked with only `squash!` (no `fixup!`) must still autosquash —
    the fast-path gate matches all three directives, not just fixup!."""
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "branch work")
    target = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "g.txt").write_text("work (more)\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", f"--squash={target}")
    # Vacuous-pass guard: the squash! commit must actually exist before the
    # script runs — if the editor-backed setup commit silently failed, the
    # final "no squash! subjects" assert would pass without testing anything.
    assert any(s.startswith("squash!") for s in _subjects(pr)), (
        "setup failed to land the squash! commit (editor-dependent setup?)"
    )

    result = _run(pr)
    assert result.returncode == 0, result.stderr
    assert not any(
        s.startswith(("fixup!", "squash!", "amend!")) for s in _subjects(pr))


def test_autosquash_noop_when_no_fixup_commits(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    # Plain commit with no fixup!/squash!/amend! subject -> fast path, nothing to fold.
    (pr / "plain.txt").write_text("plain\n")
    _git(pr, "add", "plain.txt")
    _git(pr, "commit", "-m", "plain commit, no directive")
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()
    result = _run(pr)
    assert result.returncode == 0
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before


def _build_plain_conflict(pr: Path) -> str:
    """No integration merge in range. A target commit edits a.txt; an intervening
    commit edits the SAME line; a fixup!->target edits it again. Autosquash reorders
    the fixup under its target, which conflicts on the fold.
    Returns the pre-autosquash HEAD sha."""
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "a.txt").write_text("orig\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", "-m", "target")
    target = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "a.txt").write_text("intervened\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", "-m", "intervening")
    (pr / "a.txt").write_text("fixed\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", f"--fixup={target}")
    return _git(pr, "rev-parse", "HEAD").stdout.strip()


def test_plain_path_hands_off_conflict_with_exit_2(tmp_path: Path) -> None:
    """A fixup that conflicts on reorder (no merge in range) is handed off: exit 2,
    the rebase is left IN PROGRESS with unmerged paths (NOT aborted), and the
    pre-autosquash HEAD + tree are persisted to the change-dir sentinels."""
    pr = tmp_path / "wt"
    before_head = _build_plain_conflict(pr)
    before_tree = _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip()

    result = _run(pr)
    assert result.returncode == 2, result.stderr
    rebase_dir = _git(pr, "rev-parse", "--git-path", "rebase-merge").stdout.strip()
    assert (pr / rebase_dir).is_dir()                       # rebase left in progress
    assert _git(pr, "ls-files", "-u").stdout.strip() != ""  # unmerged paths present
    state = _home(pr) / "state"
    assert (state / "autosquash-before-tree").read_text().strip() == before_tree
    assert (state / "autosquash-before-head").read_text().strip() == before_head


def test_plain_path_clears_stale_attempts_file(tmp_path: Path) -> None:
    """A fresh plain-path handoff must reset the attempt counter so a prior
    interrupted run's stale count cannot cause premature exhaustion."""
    pr = tmp_path / "wt"
    _build_plain_conflict(pr)
    state = _home(pr) / "state"
    state.mkdir(parents=True, exist_ok=True)
    (state / "autosquash-resolve-attempts").write_text("2\n")  # stale from prior run

    result = _run(pr)
    assert result.returncode == 2, result.stderr
    assert not (state / "autosquash-resolve-attempts").exists()


def test_autosquash_noop_when_cur_base_head_is_head_despite_fixup(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "branch work")
    target = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "g.txt").write_text("work (fixed)\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", f"--fixup={target}")
    head = _git(pr, "rev-parse", "HEAD").stdout.strip()
    cd = _home(pr)
    cd.mkdir(parents=True, exist_ok=True)
    (cd / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main", "cur_base_head": head}))
    res = _run(pr)
    assert res.returncode == 0, res.stderr
    # cur_base_head == HEAD => empty range => fast-path no-op; the fixup! survives.
    assert any(s.startswith("fixup!") for s in _subjects(pr))


def test_plain_path_non_conflict_failure_exits_1(tmp_path: Path) -> None:
    """A non-conflict rebase failure (a stale rebase-merge directory makes git
    refuse to start the rebase: non-zero exit, NO unmerged paths) is a hard failure,
    NOT a fold conflict — it exits 1 (routes to pause) and writes NO oracle sentinel."""
    pr = tmp_path / "wt"
    _build_plain_conflict(pr)
    rebase_dir = _git(pr, "rev-parse", "--git-path", "rebase-merge").stdout.strip()
    (pr / rebase_dir).mkdir(parents=True, exist_ok=True)    # stale: git will refuse

    result = _run(pr)
    assert result.returncode == 1, result.stderr
    assert not (_home(pr) / "state" / "autosquash-before-tree").exists()
