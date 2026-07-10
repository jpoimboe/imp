"""cleanup.sh — the end-of-change fold + stamp over cur_base_head..HEAD.

Exit contract:
  0 -> clean tree, fixups folded, Change: <id> stamped (or already-clean no-op)
  2 -> resolvable fold conflict (rebase left in progress, sentinels written)
  1 -> pause for a human: dirty working tree, a surviving fixup directive that
       cannot be folded, or a hard rebase/stamp failure

The range base is quest.json's cur_base_head (the whole change is in range), so a
fixup! targeting ANY in-range commit folds. No positional argument is used.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLEANUP = Path("src/imp/assets/workflows/imp/scripts/cleanup.sh").resolve()
ID = "2026-06-20-cleanup-test"


def _home(pr: Path) -> Path:
    return pr.parent / ".imp" / "quests" / "x"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=False, env={**os.environ, "GIT_EDITOR": "true"})


def _run(pr: Path, quest_id: str = ID) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(CLEANUP)], cwd=pr.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(pr),
             "IMP_QUEST_DIR": str(_home(pr)), "IMP_QUEST_ID": quest_id},
        capture_output=True, text=True, check=False,
    )


def _init(pr: Path) -> None:
    pr.mkdir(parents=True, exist_ok=True)
    _git(pr, "init", "-b", "main")
    _git(pr, "config", "user.email", "t@t")
    _git(pr, "config", "user.name", "t")
    (pr / ".gitignore").write_text(".imp/\n")
    _git(pr, "add", ".gitignore")
    _git(pr, "commit", "-m", "gitignore .imp/")


def _record_change(pr: Path) -> None:
    cd = _home(pr)
    cd.mkdir(parents=True, exist_ok=True)
    fork = _git(pr, "rev-parse", "main").stdout.strip()
    (cd / "quest.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main", "cur_base_head": fork}))


def _subjects(pr: Path, base: str) -> list[str]:
    out = _git(pr, "log", "--format=%s", f"{base}..HEAD").stdout
    return [s for s in out.splitlines() if s]


def _trailer(pr: Path, ref: str) -> str:
    return _git(pr, "log", "-1", "--format=%(trailers:key=Change,valueonly)", ref
                ).stdout.strip()


def _state(pr: Path) -> Path:
    return _home(pr) / "state"


# ── fold + stamp happy paths ────────────────────────────────────────────────

def test_folds_fixup_and_stamps(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    fork = _git(pr, "rev-parse", "main").stdout.strip()
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "branch work")
    target = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "g.txt").write_text("work (fixed)\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", f"--fixup={target}")

    res = _run(pr)
    assert res.returncode == 0, res.stderr
    assert not any(s.startswith(("fixup!", "squash!", "amend!"))
                   for s in _subjects(pr, fork))
    assert (pr / "g.txt").read_text() == "work (fixed)\n"
    assert _trailer(pr, "HEAD") == ID


def test_stamps_unstamped_commits(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    fork = _git(pr, "rev-parse", "main").stdout.strip()
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "a.txt").write_text("a\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", "-m", "feat a")
    (pr / "b.txt").write_text("b\n")
    _git(pr, "add", "b.txt")
    _git(pr, "commit", "-m", "feat b")

    res = _run(pr)
    assert res.returncode == 0, res.stderr
    for h in _git(pr, "rev-list", f"{fork}..HEAD").stdout.split():
        assert _trailer(pr, h) == ID, f"{h} not stamped"


def test_fixup_targeting_earlier_stamped_commit_folds(tmp_path: Path) -> None:
    """The previously-stalling case: a task_gap fix committed as a fixup! targeting
    an EARLIER, already-stamped change commit. With the whole-change range it is in
    range and folds cleanly (exit 0) instead of stalling."""
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    fork = _git(pr, "rev-parse", "main").stdout.strip()
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("orig\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", f"earlier work\n\nChange: {ID}")   # already stamped
    target = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "h.txt").write_text("later\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", "-m", f"later work\n\nChange: {ID}")      # already stamped
    (pr / "g.txt").write_text("orig (fixed)\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", f"--fixup={target}")                      # targets the earlier commit

    res = _run(pr)
    assert res.returncode == 0, res.stderr
    assert not any(s.startswith(("fixup!", "squash!", "amend!"))
                   for s in _subjects(pr, fork))
    assert (pr / "g.txt").read_text() == "orig (fixed)\n"       # fold hit the earlier commit
    assert _trailer(pr, "HEAD") == ID


def test_noop_when_clean_and_already_stamped(tmp_path: Path) -> None:
    """No fixups and every commit already stamped -> a TRUE no-op: HEAD is not
    rewritten (zero-churn on a merge-site re-entry)."""
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", f"feat work\n\nChange: {ID}")
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()

    res = _run(pr)
    assert res.returncode == 0, res.stderr
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before


# ── pause paths (exit 1) ─────────────────────────────────────────────────────

def test_dirty_tree_pauses_exit_1(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "branch work")
    (pr / "g.txt").write_text("uncommitted edit\n")          # left dirty

    res = _run(pr)
    assert res.returncode == 1, res.stderr
    assert "g.txt" in res.stderr


def test_unmatched_directive_pauses_exit_1(tmp_path: Path) -> None:
    """A fixup! whose subject matches no in-range commit survives autosquash and
    cannot be folded; cleanup pauses (exit 1) with a prescriptive diagnostic."""
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "branch work")
    (pr / "h.txt").write_text("more\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", "-m", "fixup! no such target subject")   # unmatched directive

    res = _run(pr)
    assert res.returncode == 1, res.stderr
    low = res.stderr.lower()
    assert "survived autosquash" in low
    assert "forward commit" in low


# ── fold conflict hand-off (exit 2) + verify-mode re-entry ───────────────────

def test_fold_conflict_hands_off_exit_2(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
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
    before_head = _git(pr, "rev-parse", "HEAD").stdout.strip()
    before_tree = _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip()

    res = _run(pr)
    assert res.returncode == 2, res.stderr
    rebase_dir = _git(pr, "rev-parse", "--git-path", "rebase-merge").stdout.strip()
    assert (pr / rebase_dir).is_dir()
    state = _state(pr)
    assert (state / "autosquash-before-tree").read_text().strip() == before_tree
    assert (state / "autosquash-before-head").read_text().strip() == before_head
    _git(pr, "rebase", "--abort")


def test_empty_quest_id_fails(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "branch work")

    res = _run(pr, quest_id="")
    assert res.returncode == 1                               # ${IMP_QUEST_ID:?}


def _build_and_handoff(pr: Path) -> tuple[str, str]:
    """Build a plain-path fold conflict, run cleanup.sh to its exit-2 hand-off, and
    return (before_head, before_tree) captured before the rebase started."""
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
    before_head = _git(pr, "rev-parse", "HEAD").stdout.strip()
    before_tree = _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip()
    handoff = _run(pr)
    assert handoff.returncode == 2, handoff.stderr      # precondition: mid-flight
    return before_head, before_tree


def _resolve(pr: Path, content: str) -> None:
    """Drive the mid-flight rebase to completion, resolving each stop by writing
    `content` to a.txt (the only conflicted path in this fixture)."""
    while True:
        rebase_dir = _git(pr, "rev-parse", "--git-path", "rebase-merge").stdout.strip()
        if not (pr / rebase_dir).is_dir():
            break
        (pr / "a.txt").write_text(content)
        _git(pr, "add", "a.txt")
        _git(pr, "rebase", "--continue")


def test_verify_passes_and_stamps(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _, before_tree = _build_and_handoff(pr)
    _resolve(pr, "fixed\n")                              # reproduces the pre-fold tree

    res = _run(pr)
    assert res.returncode == 0, res.stderr
    assert _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip() == before_tree
    fork = _git(pr, "rev-parse", "main").stdout.strip()
    assert not any(s.startswith(("fixup!", "squash!", "amend!"))
                   for s in _subjects(pr, fork))
    for h in _git(pr, "rev-list", f"{fork}..HEAD").stdout.split():
        assert _trailer(pr, h) == ID                    # fell through to stamp, all commits
    assert not (_state(pr) / "autosquash-before-tree").exists()   # sentinels cleaned
    assert not (_state(pr) / "autosquash-before-head").exists()
    assert not (_state(pr) / "autosquash-resolve-attempts").exists()


def test_verify_rejects_rebase_in_progress(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _build_and_handoff(pr)
    # rebase still mid-flight; do NOT resolve
    res = _run(pr)
    assert res.returncode == 2, res.stderr
    assert "in progress" in res.stderr.lower()
    assert (_state(pr) / "autosquash-resolve-attempts").read_text().strip() == "1"
    assert (_state(pr) / "autosquash-before-tree").exists()
    _git(pr, "rebase", "--abort")                       # tidy for teardown


def test_verify_rejects_remaining_directives(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _build_and_handoff(pr)
    _git(pr, "rebase", "--abort")   # no rebase in progress; fixup! commit still on branch

    res = _run(pr)
    assert res.returncode == 2, res.stderr
    assert "fixup" in res.stderr.lower()
    assert (_state(pr) / "autosquash-resolve-attempts").read_text().strip() == "1"
    assert (_state(pr) / "autosquash-before-tree").exists()


def test_verify_rejects_tree_drift(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _build_and_handoff(pr)
    _resolve(pr, "WRONG\n")                              # drops the fix: tree diverges
    res = _run(pr)
    assert res.returncode == 2, res.stderr
    assert "tree" in res.stderr.lower()
    assert (_state(pr) / "autosquash-resolve-attempts").read_text().strip() == "1"
    assert (_state(pr) / "autosquash-before-tree").exists()


def test_verify_exhaustion_restores_pristine(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    before_head, before_tree = _build_and_handoff(pr)
    _resolve(pr, "WRONG\n")
    (_state(pr) / "autosquash-resolve-attempts").write_text("2")  # this run is #3 of 3
    res = _run(pr)
    assert res.returncode == 1, res.stderr
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == before_head
    assert _git(pr, "rev-parse", "HEAD^{tree}").stdout.strip() == before_tree
    assert not (_state(pr) / "autosquash-before-tree").exists()
    assert not (_state(pr) / "autosquash-before-head").exists()
    assert not (_state(pr) / "autosquash-resolve-attempts").exists()
    assert "rebase -i --autosquash" in res.stderr        # manual-recovery hint
