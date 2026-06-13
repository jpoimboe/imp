"""autosquash.sh folds fixup! commits into their targets.

Layered behavior, all exercised here against the real shipped script + real git:

- No integration merge in range: ordinary autosquash from the fork point.
- A fixup targeting a POST-merge commit: cheap path rebases only above the most
  recent integration merge, leaving the merge (and its baked-in resolutions)
  byte-identical.
- A fixup targeting a PRE-merge commit (B3/#4): the merge must be recreated to fold
  it. autosquash does this under `git rerere` (seeded from each integration merge's
  recorded resolution) guarded by a TREE-EQUALITY invariant — folding fixups must
  never change the final tree. So an unrelated pre-merge fixup auto-folds with the
  resolution preserved, while a fixup that would drop or alter a resolution (it
  overlaps a resolved conflict, or a clean-merge semantic edit can't be replayed)
  is refused with the branch left exactly pristine."""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

AUTOSQUASH = Path("src/imp/assets/workflows/default/scripts/autosquash.sh").resolve()


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
        env={**os.environ, "IMP_PROJECT_ROOT": str(project_root)},
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
    change_dir = project_root / ".imp" / "change"
    change_dir.mkdir(parents=True, exist_ok=True)
    (change_dir / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main"}))


def _build_clean_merge(project_root: Path) -> tuple[str, str]:
    """main: A → B(f v1) → C(f v2); imp/x: A → B → D(g.txt) → M(merge C).
    Branch and base touch DIFFERENT files, so M is a clean auto-merge (no manual
    resolution). Returns (branch_work_sha D, merge_sha M)."""
    _init(project_root)
    (project_root / "f.txt").write_text("base v1\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v1")
    _git(project_root, "checkout", "-b", "imp/x")
    (project_root / "g.txt").write_text("branch work\n")
    _git(project_root, "add", "g.txt")
    _git(project_root, "commit", "-m", "branch work")
    branch_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()
    _git(project_root, "checkout", "main")
    (project_root / "f.txt").write_text("base v2\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v2")
    _git(project_root, "checkout", "imp/x")
    _git(project_root, "merge", "--no-ff", "-m", "integrate main into imp/x", "main")
    merge_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()
    _record_change(project_root)
    return branch_sha, merge_sha


def _build_conflicted_merge(project_root: Path) -> tuple[str, str]:
    """Both sides edit f.txt line 2, resolved MANUALLY in the merge (keep both).
    main: A → B(l1/l2/l3) → C(l2=BASE); imp/x: A → B → D(l2=BRANCH) → M(resolved).
    Returns (branch_work_sha D, merge_sha M)."""
    _init(project_root)
    (project_root / "f.txt").write_text("line1\nline2\nline3\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v1")
    _git(project_root, "checkout", "-b", "imp/x")
    (project_root / "f.txt").write_text("line1\nBRANCH-line2\nline3\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "branch work")
    branch_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()
    _git(project_root, "checkout", "main")
    (project_root / "f.txt").write_text("line1\nBASE-line2\nline3\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v2")
    _git(project_root, "checkout", "imp/x")
    _git(project_root, "merge", "--no-ff", "--no-commit", "main")  # conflicts
    (project_root / "f.txt").write_text("line1\nBRANCH-line2\nBASE-line2\nline3\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "--no-edit")
    merge_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()
    _record_change(project_root)
    return branch_sha, merge_sha


def _build_semantic_merge(project_root: Path) -> tuple[str, str]:
    """Clean auto-merge, but the integrator bakes a SEMANTIC edit into the merge
    commit (sem.txt) that git's auto-merge would not produce. main touches f.txt,
    branch touches g.txt. Returns (branch_work_sha D, merge_sha M)."""
    _init(project_root)
    (project_root / "f.txt").write_text("a\n")
    (project_root / "sem.txt").write_text("x\n")
    _git(project_root, "add", "f.txt", "sem.txt")
    _git(project_root, "commit", "-m", "base v1")
    _git(project_root, "checkout", "-b", "imp/x")
    (project_root / "g.txt").write_text("branch work\n")
    _git(project_root, "add", "g.txt")
    _git(project_root, "commit", "-m", "branch work")
    branch_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()
    _git(project_root, "checkout", "main")
    (project_root / "f.txt").write_text("b\n")
    _git(project_root, "add", "f.txt")
    _git(project_root, "commit", "-m", "base v2")
    _git(project_root, "checkout", "imp/x")
    _git(project_root, "merge", "--no-ff", "--no-commit", "main")  # clean
    (project_root / "sem.txt").write_text("SEMANTIC-FIX\n")  # not from auto-merge
    _git(project_root, "add", "sem.txt")
    _git(project_root, "commit", "--no-edit")
    merge_sha = _git(project_root, "rev-parse", "HEAD").stdout.strip()
    _record_change(project_root)
    return branch_sha, merge_sha


def _subjects(pr: Path) -> list[str]:
    fork = _git(pr, "merge-base", "main", "HEAD").stdout.strip()
    out = _git(pr, "log", "--format=%s", f"{fork}..HEAD").stdout
    return [s for s in out.splitlines() if s]


def _merges(pr: Path) -> int:
    fork = _git(pr, "merge-base", "main", "HEAD").stdout.strip()
    out = _git(pr, "rev-list", "--merges", f"{fork}..HEAD").stdout
    return len([s for s in out.splitlines() if s])


def _tree(pr: Path) -> str:
    return _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip()


def _rr_cache_count(pr: Path) -> int:
    rr = pr / ".git" / "rr-cache"
    return len(list(rr.iterdir())) if rr.is_dir() else 0


# ── cheap path: post-merge fixup ──────────────────────────────────────────────

def test_autosquash_folds_post_merge_fixup_and_preserves_merge(tmp_path: Path) -> None:
    """A fixup! targeting a POST-merge commit is folded by rebasing only the
    segment above the integration merge. The merge commit is left untouched."""
    pr = tmp_path / "wt"
    _, merge_sha = _build_clean_merge(pr)
    (pr / "h.txt").write_text("post v1\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", "-m", "post-merge work")
    post_sha = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "h.txt").write_text("post v1 (fixed)\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", f"--fixup={post_sha}")

    result = _run(pr)
    assert result.returncode == 0, result.stderr
    assert _git(pr, "merge-base", "--is-ancestor", merge_sha, "HEAD").returncode == 0
    assert _git(pr, "rev-list", "--merges", "--max-count=1",
                f"{merge_sha}^..HEAD").stdout.strip() == merge_sha
    assert not any(s.startswith("fixup!") for s in _subjects(pr))
    assert (pr / "h.txt").read_text() == "post v1 (fixed)\n"


# ── #4: pre-merge fixup, merge-preserving auto-fold ───────────────────────────

def test_autosquash_folds_pre_merge_fixup_unrelated_to_resolution(tmp_path: Path) -> None:
    """A fixup! targeting a PRE-merge commit, touching a file unrelated to the
    integration, is auto-folded: the merge is recreated under rerere and the final
    tree is preserved. The fixup's content lands and the merge survives."""
    pr = tmp_path / "wt"
    branch_sha, _ = _build_conflicted_merge(pr)
    (pr / "h.txt").write_text("post\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", "-m", "post-merge work")
    # fixup! -> D (pre-merge), editing g.txt, which the f.txt merge never touched.
    (pr / "g.txt").write_text("branch extra\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", f"--fixup={branch_sha}")
    before_tree = _tree(pr)

    result = _run(pr)
    assert result.returncode == 0, result.stderr
    assert _tree(pr) == before_tree                 # fold preserved the final tree
    assert _merges(pr) >= 1                          # merge recreated, not linearized
    assert not any(s.startswith("fixup!") for s in _subjects(pr))
    # The integrator's manual f.txt resolution survived.
    assert (pr / "f.txt").read_text() == "line1\nBRANCH-line2\nBASE-line2\nline3\n"
    assert (pr / "g.txt").read_text() == "branch extra\n"
    assert _rr_cache_count(pr) == 0                  # rerere cache cleaned up


def test_autosquash_pauses_when_fixup_overlaps_resolved_region(tmp_path: Path) -> None:
    """The dangerous case: a pre-merge fixup edits the SAME region the integrator
    resolved by hand. rerere can't replay onto the changed preimage, so the
    recreate conflicts — autosquash refuses and leaves the branch exactly
    pristine. No silent corruption."""
    pr = tmp_path / "wt"
    branch_sha, _ = _build_conflicted_merge(pr)
    (pr / "h.txt").write_text("post\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", "-m", "post-merge work")
    # fixup! -> D editing f.txt line 2 — the resolved conflict region.
    (pr / "f.txt").write_text("line1\nBRANCH-line2-EDITED\nline3\n")
    _git(pr, "add", "f.txt")
    _git(pr, "commit", f"--fixup={branch_sha}")
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()

    result = _run(pr)
    assert result.returncode == 1
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before  # pristine
    assert any(s.startswith("fixup!") for s in _subjects(pr))           # nothing folded
    assert _rr_cache_count(pr) == 0                                     # cache cleaned up


def test_autosquash_pauses_when_fold_would_drop_a_semantic_resolution(tmp_path: Path) -> None:
    """A clean auto-merge with a SEMANTIC edit baked in: recreating the merge
    re-runs the clean auto-merge, which lacks the integrator's edit. rerere learns
    nothing (no conflict), so the tree-equality guard is what catches it — the
    final tree would differ, so autosquash refuses pristine."""
    pr = tmp_path / "wt"
    branch_sha, _ = _build_semantic_merge(pr)
    (pr / "h.txt").write_text("post\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", "-m", "post-merge work")
    (pr / "g.txt").write_text("branch extra\n")  # unrelated pre-merge fixup
    _git(pr, "add", "g.txt")
    _git(pr, "commit", f"--fixup={branch_sha}")
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()

    result = _run(pr)
    assert result.returncode == 1
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before  # pristine
    assert (pr / "sem.txt").read_text() == "SEMANTIC-FIX\n"             # edit intact
    assert _rr_cache_count(pr) == 0


# ── degenerate paths ──────────────────────────────────────────────────────────

def test_autosquash_noop_without_fixups_keeps_merge(tmp_path: Path) -> None:
    """Rework-after-integration WITHOUT fixup! commits: exit 0, HEAD untouched."""
    pr = tmp_path / "wt"
    _build_clean_merge(pr)
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()
    result = _run(pr)
    assert result.returncode == 0, result.stderr
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before


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


def _build_plain_conflict(pr: Path) -> str:
    """No integration merge in range. A target commit edits a.txt; an intervening
    commit edits the SAME line; a fixup!->target edits it again. Autosquash reorders
    the fixup under its target, which conflicts on the fold (and again when the
    intervening commit replays). Returns the pre-autosquash HEAD sha."""
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
    state = pr / ".imp" / "change" / "state"
    assert (state / "autosquash-before-tree").read_text().strip() == before_tree
    assert (state / "autosquash-before-head").read_text().strip() == before_head


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
    assert not (pr / ".imp" / "change" / "state" / "autosquash-before-tree").exists()
