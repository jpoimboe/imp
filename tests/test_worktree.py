# tests/test_worktree.py
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
        assert (tmp_path / ".imp" / "repo.lock").exists()
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


def test_merge_base_dirty_backs_off(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    wt.create_worktree(tmp_path, "feat", "main")
    _commit_on_branch(tmp_path, "imp/feat", "feature.txt", "hi")
    (tmp_path / "README.md").write_text("dirty edit")  # uncommitted on main

    result = wt.merge_to_base(tmp_path, "feat", "main", title="Feature")
    assert result.status == "BASE_DIRTY"
    assert not (tmp_path / "feature.txt").exists()  # nothing advanced


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


def test_journal_append_and_accumulate(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    stage = tmp_path / ".imp"
    stage.mkdir(exist_ok=True)  # gitignored by _init_repo
    f1 = stage / "events.jsonl"
    f1.write_text('{"e":1}\n')
    f2 = stage / "change.json"
    f2.write_text('{"slug":"one"}')

    # First journaled Change: ref does not exist yet -> root commit, no -p.
    wt.journal_commit(tmp_path, "one", {"events.jsonl": f1, "change.json": f2})
    assert _git(tmp_path, "show", "refs/imp/journal:one/change.json").stdout == '{"slug":"one"}'

    # Second Change accumulates under its own slug dir.
    f3 = stage / "two.jsonl"
    f3.write_text('{"e":2}\n')
    wt.journal_commit(tmp_path, "two", {"events.jsonl": f3})
    assert _git(tmp_path, "show", "refs/imp/journal:two/events.jsonl").stdout == '{"e":2}\n'
    assert _git(tmp_path, "show", "refs/imp/journal:one/change.json").returncode == 0
    # Journal never touches base's working tree.
    assert _git(tmp_path, "status", "--porcelain").stdout.strip() == ""


def test_journal_not_on_code_history(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    f1 = tmp_path / "events.jsonl"
    f1.write_text("x\n")
    wt.journal_commit(tmp_path, "one", {"events.jsonl": f1})
    # Orphan: no shared ancestor with main.
    base = _git(tmp_path, "merge-base", "main", "refs/imp/journal").returncode
    assert base != 0


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
