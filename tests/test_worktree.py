# tests/test_worktree.py
import inspect
import subprocess
from pathlib import Path

import pytest

from imp.engine import worktree as wt


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _init_repo(root: Path) -> None:
    """A git repo with one commit on branch `main` and `.imp/` gitignored."""
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def test_find_control_root_from_subdir(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    sub = tmp_path / "a" / "b"
    sub.mkdir(parents=True)
    assert wt.find_control_root(sub) == tmp_path.resolve()


def test_preconditions_ok(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.check_preconditions(tmp_path)  # does not raise


def test_preconditions_not_a_repo(tmp_path: Path) -> None:
    with pytest.raises(wt.PreconditionError, match="not a git repository"):
        wt.check_preconditions(tmp_path)


def test_preconditions_no_commits(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    with pytest.raises(wt.PreconditionError, match="no commits"):
        wt.check_preconditions(tmp_path)


def test_preconditions_detached_head(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    head = _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    _git(tmp_path, "checkout", "-q", head)  # detach
    with pytest.raises(wt.PreconditionError, match="detached"):
        wt.check_preconditions(tmp_path)


def test_repo_git_lock_serializes(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    lock = wt.RepoGitLock(tmp_path)
    with lock.acquire():
        assert (tmp_path / ".git" / "imp" / "repo.lock").exists()
    with lock.acquire():  # re-acquire after release
        pass


def test_create_worktree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = wt.create_worktree(tmp_path, "add-oauth", "main")
    assert path == tmp_path / ".imp" / "worktrees" / "add-oauth"
    assert path.is_dir()
    assert (path / "README.md").exists()  # checked out from main
    branches = _git(tmp_path, "branch", "--list", "imp/add-oauth").stdout
    assert "imp/add-oauth" in branches


def test_create_worktree_branch_collision(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.create_worktree(tmp_path, "dup", "main")
    with pytest.raises(wt.BranchExists):
        wt.create_worktree(tmp_path, "dup", "main")


def test_find_control_root_from_inside_worktree(tmp_path: Path) -> None:
    # The resume path relies on resolving the control root from INSIDE a linked
    # worktree (spec §5.3) — not just a subdir of the main checkout.
    _init_repo(tmp_path)
    wpath = wt.create_worktree(tmp_path, "feat", "main")
    assert wt.find_control_root(wpath) == tmp_path.resolve()
    sub = wpath / ".imp" / "change"
    sub.mkdir(parents=True)
    assert wt.find_control_root(sub) == tmp_path.resolve()


def test_remove_worktree_keeps_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = wt.create_worktree(tmp_path, "gone", "main")
    wt.remove_worktree(tmp_path, "gone")
    assert not path.exists()
    # Branch is KEPT (spec §4, §5 item 5).
    assert "imp/gone" in _git(tmp_path, "branch", "--list", "imp/gone").stdout


def _commit_on_branch(root: Path, branch: str, name: str, body: str) -> None:
    """Add+commit one file directly in the change worktree the test already
    created for <branch>, so the control root's checkout stays untouched."""
    wtpath = root / ".imp" / "worktrees" / branch.removeprefix("imp/")
    (wtpath / name).write_text(body)
    _git(wtpath, "add", "-A")
    _git(wtpath, "commit", "-qm", f"add {name}")


def test_merge_base_checked_out_clean(tmp_path: Path) -> None:
    _init_repo(tmp_path)  # control root sits on clean main
    wt.create_worktree(tmp_path, "feat", "main")
    _commit_on_branch(tmp_path, "imp/feat", "feature.txt", "hi")

    result = wt.merge_to_base(tmp_path, "feat", "main", title="My Feature")
    assert result.status == "MERGED"
    assert result.merge_commit == _git(tmp_path, "rev-parse", "HEAD").stdout.strip()
    # main's working tree advanced together with the ref (no desync).
    assert (tmp_path / "feature.txt").read_text() == "hi"
    # REGRESSION (spec §11): control-root status is NOT a staged reversal.
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == ""
    # Merge subject uses the human title; id appears in the body (AC 9).
    subject = _git(tmp_path, "log", "-1", "--format=%B", result.merge_commit).stdout
    assert "Merge My Feature" in subject
    assert "(imp/feat)" in subject


def _seed_base_and_branch(root: Path) -> None:
    """Init a repo at `root` on `main` with `.imp/` gitignored, an initial commit
    containing `shared.txt`, and branch `imp/feat` that commits a change to
    `shared.txt`. Leaves `main` checked out at `root`."""
    _init_repo(root)  # main branch, .imp/ gitignored, README.md committed
    # Commit shared.txt on main
    (root / "shared.txt").write_text("original\n")
    _git(root, "add", "shared.txt")
    _git(root, "commit", "-qm", "add shared.txt")
    # Create imp/feat branch and commit a change to shared.txt
    wt.create_worktree(root, "feat", "main")
    wtpath = root / ".imp" / "worktrees" / "feat"
    (wtpath / "shared.txt").write_text("branch version\n")
    _git(wtpath, "add", "shared.txt")
    _git(wtpath, "commit", "-qm", "update shared.txt on branch")
    # main is still checked out at root


def test_merge_proceeds_when_base_dirt_does_not_overlap(tmp_path: Path) -> None:
    # Base checkout has an unstaged change to a TRACKED file the merge does NOT
    # touch; git merges cleanly. The old BASE_DIRTY blanket pre-check refused on
    # any dirt at all.
    _seed_base_and_branch(tmp_path)
    # Commit a tracked unrelated file on main, then leave an unstaged edit to it.
    (tmp_path / "unrelated.txt").write_text("committed\n")
    _git(tmp_path, "add", "unrelated.txt")
    _git(tmp_path, "commit", "-m", "add unrelated")
    (tmp_path / "unrelated.txt").write_text("uncommitted edit\n")
    result = wt.merge_to_base(tmp_path, "feat", "main", title="Feature")
    assert result.status == "MERGED"
    assert result.merge_commit is not None
    # The unstaged edit to the unrelated file must be preserved — the key invariant
    # distinguishing the new per-file check from the old blanket pre-check.
    assert (tmp_path / "unrelated.txt").read_text() == "uncommitted edit\n"
    # shared.txt should have advanced to the branch version after the merge.
    assert (tmp_path / "shared.txt").read_text() == "branch version\n"


def test_merge_overwrite_refusal_does_not_abort(tmp_path: Path) -> None:
    # Base checkout has an unstaged change to a file the merge WOULD overwrite.
    # git refuses before starting a merge; we must NOT call `git merge --abort`
    # (there is no merge to abort) and must report REFUSED, not CONFLICT.
    _seed_base_and_branch(tmp_path)
    # 'shared.txt' is modified on the branch (part of the merge) AND dirty on base.
    (tmp_path / "shared.txt").write_text("uncommitted base edit\n")
    result = wt.merge_to_base(tmp_path, "feat", "main", title="Feature")
    assert result.status == "REFUSED"
    assert result.detail is not None  # git writes a refusal reason to stderr
    # The base working tree is untouched (git refused atomically).
    assert (tmp_path / "shared.txt").read_text() == "uncommitted base edit\n"


def test_merge_base_checked_out_nowhere(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.create_worktree(tmp_path, "feat", "main")
    _commit_on_branch(tmp_path, "imp/feat", "feature.txt", "hi")
    # Park the control root on another branch so `main` is checked out NOWHERE.
    _git(tmp_path, "checkout", "-q", "-b", "parking")

    base_before = _git(tmp_path, "rev-parse", "main").stdout.strip()
    result = wt.merge_to_base(tmp_path, "feat", "main", title="Feature")
    assert result.status == "MERGED"
    assert _git(tmp_path, "rev-parse", "main").stdout.strip() != base_before  # ref moved
    assert result.merge_commit == _git(tmp_path, "rev-parse", "main").stdout.strip()
    # scratch worktree cleaned up
    assert not (tmp_path / ".imp" / "worktrees" / ".merge-feat").exists()


def test_merge_detached_conflict_cleans_scratch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.create_worktree(tmp_path, "feat", "main")
    _commit_on_branch(tmp_path, "imp/feat", "README.md", "branch version")
    # Advance main with a conflicting edit (control root is on main), then park
    # the control root on another branch so main is checked out NOWHERE — this
    # forces merge_to_base down the _merge_detached path.
    (tmp_path / "README.md").write_text("base version")
    _git(tmp_path, "commit", "-qam", "base edit")
    _git(tmp_path, "checkout", "-q", "-b", "parking")  # main now checked out nowhere

    result = wt.merge_to_base(tmp_path, "feat", "main", title="Feature")
    assert result.status == "CONFLICT"
    assert not (tmp_path / ".imp" / "worktrees" / ".merge-feat").exists()  # scratch cleaned
    assert (tmp_path / ".imp" / "worktrees" / "feat").is_dir()             # branch worktree intact


def test_merge_detached_body_threads_into_commit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.create_worktree(tmp_path, "feat", "main")
    _commit_on_branch(tmp_path, "imp/feat", "feature.txt", "hi")
    # Park control root so main is checked out nowhere — forces _merge_detached.
    _git(tmp_path, "checkout", "-q", "-b", "parking")
    body = "We chose X over Y because Z."
    result = wt.merge_to_base(tmp_path, "feat", "main", title="My Feature", body=body)
    assert result.status == "MERGED"
    msg = _git(tmp_path, "log", "-1", "--format=%B", "main").stdout
    assert "Merge My Feature" in msg
    assert "We chose X over Y because Z." in msg
    assert "(imp/feat)" in msg


def test_merge_conflict_aborts_and_preserves(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # Conflict: both base and branch edit README.md differently.
    wt.create_worktree(tmp_path, "feat", "main")
    _commit_on_branch(tmp_path, "imp/feat", "README.md", "branch version")
    # advance main (in place on the control root) with a conflicting edit
    (tmp_path / "README.md").write_text("base version")
    _git(tmp_path, "commit", "-qam", "base edit")

    result = wt.merge_to_base(tmp_path, "feat", "main", title="Feature")
    assert result.status == "CONFLICT"
    # branch + change worktree untouched, no merge state lingering
    assert (tmp_path / ".imp" / "worktrees" / "feat").is_dir()
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == ""


def _make_worktree_and_check(root, slug="probe") -> bool:
    """True iff a fresh worktree off base HEAD ignores .imp/."""
    wt.create_worktree(root, slug, "main")
    wpath = root / ".imp" / "worktrees" / slug
    return _git(wpath, "check-ignore", ".imp/change").returncode == 0


def test_ensure_imp_ignored_committed_rule_noop(tmp_path: Path) -> None:
    _init_repo(tmp_path)  # .gitignore with .imp/ is COMMITTED -> durable
    before = (tmp_path / ".gitignore").read_text()
    wt.ensure_imp_ignored(tmp_path)
    assert (tmp_path / ".gitignore").read_text() == before          # no new commit
    assert _make_worktree_and_check(tmp_path)


def test_ensure_imp_ignored_uncommitted_rule_is_made_durable(tmp_path: Path) -> None:
    # The fresh-`imp init` shape: .imp/ appended to .gitignore but NOT committed.
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    (tmp_path / ".gitignore").write_text(".imp/\n")  # uncommitted

    wt.ensure_imp_ignored(tmp_path)
    # Now durable AND the worktree actually ignores .imp/ (the real invariant).
    assert _make_worktree_and_check(tmp_path)


def test_ensure_imp_ignored_repairs_mis_set_committed_rule(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / ".gitignore").write_text(".imp/changes/\n.imp/config.yaml\n")  # sub-paths only
    (tmp_path / "README.md").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")

    wt.ensure_imp_ignored(tmp_path)
    assert _make_worktree_and_check(tmp_path)


def test_destroy_worktree_removes_worktree_and_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    path = wt.create_worktree(tmp_path, "gone", "main")
    assert path.exists()
    assert wt.branch_exists(tmp_path, "imp/gone")
    wt.destroy_worktree(tmp_path, "gone")
    assert not path.exists()
    assert not wt.branch_exists(tmp_path, "imp/gone")



def test_artifact_commit_snapshots_full_tree_with_guard(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    stage = tmp_path / ".imp"
    stage.mkdir(exist_ok=True)
    spec = stage / "spec.md"
    spec.write_text("# the spec\n")
    plan = stage / "plan.md"
    plan.write_text("# plan 1\n")

    # First snapshot: ref does not exist yet -> root commit; returns True.
    assert wt.artifact_commit(
        tmp_path, "feat", {"spec/feat.md": spec}, message="artifact: spec") is True
    assert _git(tmp_path, "show",
                "refs/imp/artifacts/feat:spec/feat.md").stdout == "# the spec\n"
    first = _git(tmp_path, "rev-parse", "refs/imp/artifacts/feat").stdout.strip()

    # Second snapshot carries the FULL set: the plan is added, the spec survives,
    # and the new commit is parented on the first (lineage grows, not replaced).
    assert wt.artifact_commit(
        tmp_path, "feat",
        {"spec/feat.md": spec, "plans/feat-plan-1.md": plan},
        message="artifact: plan") is True
    assert _git(tmp_path, "show",
                "refs/imp/artifacts/feat:plans/feat-plan-1.md").stdout == "# plan 1\n"
    assert _git(tmp_path, "show",
                "refs/imp/artifacts/feat:spec/feat.md").returncode == 0
    second = _git(tmp_path, "rev-parse", "refs/imp/artifacts/feat").stdout.strip()
    assert second != first
    assert _git(tmp_path, "rev-parse",
                "refs/imp/artifacts/feat~1").stdout.strip() == first

    # Re-snapshotting the identical set is a no-op: returns False, ref unmoved.
    assert wt.artifact_commit(
        tmp_path, "feat",
        {"spec/feat.md": spec, "plans/feat-plan-1.md": plan},
        message="artifact: plan") is False
    assert _git(tmp_path, "rev-parse",
                "refs/imp/artifacts/feat").stdout.strip() == second

    # Dropping the plan from the set deletes it at the tip (full snapshot), while
    # the spec remains — this is what distinguishes snapshot from accumulate.
    assert wt.artifact_commit(
        tmp_path, "feat", {"spec/feat.md": spec}, message="artifact: spec") is True
    assert _git(tmp_path, "rev-parse", "--verify", "--quiet",
                "refs/imp/artifacts/feat:plans/feat-plan-1.md").returncode != 0
    assert _git(tmp_path, "show",
                "refs/imp/artifacts/feat:spec/feat.md").returncode == 0

    # Files live FLAT at the tree root (no <id>/ prefix) and the code tree is untouched.
    assert "feat/" not in _git(tmp_path, "ls-tree", "--name-only",
                               "refs/imp/artifacts/feat").stdout
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == ""


def test_artifact_commit_empty_files_is_noop(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert wt.artifact_commit(tmp_path, "feat", {}, message="artifact: spec") is False
    assert _git(tmp_path, "rev-parse", "--verify", "--quiet",
                "refs/imp/artifacts/feat").returncode != 0


def test_ensure_imp_ignored_exclude_fallback_when_commit_blocked(tmp_path: Path) -> None:
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / "README.md").write_text("x")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "seed")
    # Force the .gitignore commit to fail -> exercises the info/exclude fallback.
    hook = tmp_path / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    wt.ensure_imp_ignored(tmp_path)
    exclude = wt.find_control_root(tmp_path) / ".git" / "info" / "exclude"
    assert ".imp/" in exclude.read_text()
    assert _make_worktree_and_check(tmp_path)


def test_merge_body_carries_decisions(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.create_worktree(tmp_path, "feat", "main")
    _commit_on_branch(tmp_path, "imp/feat", "feature.txt", "hi")
    body = "We chose X over Y because Z."
    result = wt.merge_to_base(tmp_path, "feat", "main", title="My Feature", body=body)
    assert result.status == "MERGED"
    msg = _git(tmp_path, "log", "-1", "--format=%B", result.merge_commit).stdout
    assert "Merge My Feature" in msg
    assert "We chose X over Y because Z." in msg
    assert "(imp/feat)" in msg


def test_merge_body_none_keeps_minimal(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.create_worktree(tmp_path, "feat", "main")
    _commit_on_branch(tmp_path, "imp/feat", "feature.txt", "hi")
    result = wt.merge_to_base(tmp_path, "feat", "main", title="My Feature", body=None)
    assert result.status == "MERGED"
    msg = _git(tmp_path, "log", "-1", "--format=%B", result.merge_commit).stdout.strip()
    # Minimal body: subject + the id trailer only.
    assert msg == "Merge My Feature\n\n(imp/feat)"


def test_merge_to_base_is_idempotent_on_already_merged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.create_worktree(tmp_path, "feat", "main")
    wt_path = (tmp_path / ".imp" / "worktrees" / "feat")
    (wt_path / "x.txt").write_text("hi\n")
    _git(wt_path, "add", "-A")
    _git(wt_path, "commit", "-qm", "feat")

    r1 = wt.merge_to_base(tmp_path, "feat", "main", title="feat")
    assert r1.status == "MERGED" and r1.merge_commit
    # Re-running the merge of an already-absorbed branch is a no-op MERGED
    r2 = wt.merge_to_base(tmp_path, "feat", "main", title="feat")
    assert r2.status == "MERGED"


def test_merge_to_base_has_no_expected_base_sha_param() -> None:
    sig = inspect.signature(wt.merge_to_base)
    assert "expected_base_sha" not in sig.parameters


def test_find_control_root_not_a_repo_points_at_imp_init(tmp_path: Path) -> None:
    with pytest.raises(wt.PreconditionError) as ei:
        wt.find_control_root(tmp_path)
    msg = str(ei.value)
    assert "not an imp project" in msg
    assert "imp init" in msg


def test_preconditions_message_drops_git_init_hint(tmp_path: Path) -> None:
    with pytest.raises(wt.PreconditionError) as ei:
        wt.check_preconditions(tmp_path)
    msg = str(ei.value)
    assert "not a git repository" in msg   # existing wording kept
    assert "git init" not in msg           # the parenthetical hint is gone


def test_create_spike_worktree_distinct_from_change_worktree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    # the change's OWN worktree
    wt.create_worktree(tmp_path, "chg", "main")
    # a disposable spike worktree for the same change never collides
    spike = wt.create_spike_worktree(tmp_path, "chg", 1, "main")
    assert spike == (tmp_path / ".imp" / "worktrees" / "spike-chg-1").resolve()
    assert spike.is_dir()
    assert (spike / "README.md").exists()   # checked out from main's tip
    assert "imp/spike-chg-1" in _git(
        tmp_path, "branch", "--list", "imp/spike-chg-1").stdout
    # the change's own worktree is untouched
    assert (tmp_path / ".imp" / "worktrees" / "chg").is_dir()


def test_destroy_spike_worktree_removes_worktree_and_branch(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    spike = wt.create_spike_worktree(tmp_path, "chg", 2, "main")
    assert spike.is_dir()
    wt.destroy_spike_worktree(tmp_path, "chg", 2)
    assert not spike.exists()
    assert "imp/spike-chg-2" not in _git(
        tmp_path, "branch", "--list", "imp/spike-chg-2").stdout
    assert "spike-chg-2" not in _git(tmp_path, "worktree", "list").stdout


def test_uncommitted_paths_empty_when_clean_lists_untracked_when_dirty(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert wt.uncommitted_paths(tmp_path) == ""      # committed seed => clean
    assert wt.is_dirty(tmp_path) is False
    (tmp_path / "scratch.txt").write_text("uncommitted\n")
    out = wt.uncommitted_paths(tmp_path)
    assert "scratch.txt" in out                      # untracked file surfaced
    assert wt.is_dirty(tmp_path) is True              # boolean view agrees


def test_is_worktree_true_in_repo_false_on_bare_dir(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    assert wt.is_worktree(repo) is True               # a real checkout
    bare = tmp_path / "bare"
    bare.mkdir()                                       # plain dir, never git-inited
    assert wt.is_worktree(bare) is False              # non-repo => not a worktree

    truly_bare = tmp_path / "truly-bare"
    truly_bare.mkdir()
    subprocess.run(["git", "init", "--bare", str(truly_bare)], check=True, capture_output=True)
    assert wt.is_worktree(truly_bare) is False        # bare repo: rev-parse says "false"

