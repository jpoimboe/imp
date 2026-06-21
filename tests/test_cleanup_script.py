"""cleanup.sh — the unified clean-tree -> fold -> stamp gate over (arg or fork)..HEAD.

Exit contract:
  0 -> clean tree, fixups folded, Change: <id> stamped (or already-clean no-op)
  2 -> resolvable fold conflict (rebase left in progress, sentinels written)
  3 -> dirty working tree (uncommitted changes)
  1 -> hard failure / out-of-range fixup target

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


def test_out_of_range_fixup_exits_1(tmp_path: Path) -> None:
    """A fixup! whose target is at or below the lower bound stays unfolded; the
    post-fold directive check trips -> exit 1 (pause)."""
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
    assert res.returncode == 1, res.stderr
    assert "fixup" in res.stderr.lower()


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
