"""cleanup.sh — the unified clean-tree -> fold -> stamp gate over (arg or fork)..HEAD.

Exit contract:
  0 -> clean tree, fixups folded, Change: <id> stamped (or already-clean no-op)
  2 -> resolvable fold conflict (rebase left in progress, sentinels written)
  3 -> dirty working tree (uncommitted changes), OR a surviving fixup directive bounced
  1 -> hard failure / surviving-directive bounce cap exhausted

The lower bound is the first positional argument (the workflow passes
`{{ vars.review_base }}`); an empty argument falls back to change.json cur_base_head.
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

CLEANUP = Path("src/imp/assets/workflows/imp/scripts/cleanup.sh").resolve()
ID = "2026-06-20-cleanup-test"


def _home(pr: Path) -> Path:
    return pr.parent / ".imp" / "changes" / "x"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=False, env={**os.environ, "GIT_EDITOR": "true"})


def _run(pr: Path, lower: str = "", change_id: str = ID
         ) -> subprocess.CompletedProcess[str]:
    # The workflow always passes a positional lower bound (possibly empty).
    return subprocess.run(
        ["bash", str(CLEANUP), lower], cwd=pr.parent,
        env={**os.environ, "IMP_PROJECT_ROOT": str(pr),
             "IMP_CHANGE_DIR": str(_home(pr)), "IMP_CHANGE_ID": change_id},
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
    (cd / "change.json").write_text(json.dumps(
        {"id": "x", "title": "t", "base_branch": "main", "cur_base_head": fork}))


def _subjects(pr: Path, lower: str) -> list[str]:
    out = _git(pr, "log", "--format=%s", f"{lower}..HEAD").stdout
    return [s for s in out.splitlines() if s]


def _trailer(pr: Path, ref: str) -> str:
    return _git(pr, "log", "-1", "--format=%(trailers:key=Change,valueonly)", ref
                ).stdout.strip()


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
    assert (pr / "g.txt").read_text() == "work (fixed)\n"     # fix applied
    assert _trailer(pr, "HEAD") == ID                         # stamped


def test_stamps_unstamped_commits(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    fork = _git(pr, "rev-parse", "main").stdout.strip()
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "a.txt").write_text("a\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", "-m", "feat a")        # no trailer
    (pr / "b.txt").write_text("b\n")
    _git(pr, "add", "b.txt")
    _git(pr, "commit", "-m", "feat b")        # no trailer

    res = _run(pr)
    assert res.returncode == 0, res.stderr
    for h in _git(pr, "rev-list", f"{fork}..HEAD").stdout.split():
        assert _trailer(pr, h) == ID, f"{h} not stamped"


def test_noop_when_clean_and_already_stamped(tmp_path: Path) -> None:
    """No fixups and every commit already stamped -> a TRUE no-op: HEAD is not
    rewritten (no needless SHA churn on a merge-site re-entry)."""
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


def test_dirty_tree_exits_3(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "branch work")
    (pr / "g.txt").write_text("uncommitted edit\n")          # left dirty

    res = _run(pr)
    assert res.returncode == 3, res.stderr
    assert "g.txt" in res.stderr


def test_out_of_range_fixup_bounces_exit_3(tmp_path: Path) -> None:
    """A fixup! whose target is at or below the lower bound stays unfolded; cleanup
    now BOUNCES it back to the committing agent (exit 3) with a prescriptive
    diagnostic naming the offending commit, its target, and the forward-fix remedy."""
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("below\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "below the bound")
    target = _git(pr, "rev-parse", "HEAD").stdout.strip()    # the eventual lower bound
    (pr / "h.txt").write_text("in range\n")
    _git(pr, "add", "h.txt")
    _git(pr, "commit", "-m", "in range")
    (pr / "g.txt").write_text("below (fixed)\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", f"--fixup={target}")                  # target is AT the bound

    res = _run(pr, lower=target)
    assert res.returncode == 3, res.stderr
    low = res.stderr.lower()
    assert "fixup! below the bound" in res.stderr            # names the offending commit
    assert "below the change fork" in low                    # names the out-of-range cause
    assert "forward commit" in low                           # prescribes the remedy


def test_unmatched_directive_reports_typo(tmp_path: Path) -> None:
    """A fixup! whose subject matches no commit survives autosquash; cleanup bounces
    (exit 3) and reports it as a likely typo with no matching commit."""
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
    assert res.returncode == 3, res.stderr
    low = res.stderr.lower()
    assert "typo" in low
    assert "no commit" in low


def test_explicit_lower_bounds_the_range(tmp_path: Path) -> None:
    """cleanup never touches a commit at or below the lower bound: a commit below it
    keeps no trailer; an in-range commit is stamped."""
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "below.txt").write_text("below\n")
    _git(pr, "add", "below.txt")
    _git(pr, "commit", "-m", "below the bound")
    bound = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "above.txt").write_text("above\n")
    _git(pr, "add", "above.txt")
    _git(pr, "commit", "-m", "above the bound")
    above = _git(pr, "rev-parse", "HEAD").stdout.strip()

    res = _run(pr, lower=bound)
    assert res.returncode == 0, res.stderr
    assert _trailer(pr, bound) == ""                         # below: untouched
    assert _trailer(pr, "HEAD") == ID                        # above: stamped
    # The below-bound commit's SHA is unchanged (not rebased).
    assert _git(pr, "rev-parse", "HEAD~1").stdout.strip() == bound
    assert above != _git(pr, "rev-parse", "HEAD").stdout.strip()  # above was re-stamped


def test_empty_arg_falls_back_to_fork(tmp_path: Path) -> None:
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

    res = _run(pr, lower="")                                 # empty -> fork fallback
    assert res.returncode == 0, res.stderr
    assert not any(s.startswith("fixup!") for s in _subjects(pr, fork))
    assert _trailer(pr, "HEAD") == ID


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
    assert (pr / rebase_dir).is_dir()                        # rebase left in progress
    state = _home(pr) / "state"
    assert (state / "autosquash-before-tree").read_text().strip() == before_tree
    assert (state / "autosquash-before-head").read_text().strip() == before_head
    _git(pr, "rebase", "--abort")                            # tidy for teardown


def test_empty_change_id_fails(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    (pr / "g.txt").write_text("work\n")
    _git(pr, "add", "g.txt")
    _git(pr, "commit", "-m", "branch work")

    res = _run(pr, change_id="")
    assert res.returncode != 0                               # ${IMP_CHANGE_ID:?}


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


def _state(pr: Path) -> Path:
    return _home(pr) / "state"


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
    assert _trailer(pr, "HEAD") == ID                   # fell through to stamp
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
