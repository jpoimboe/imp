"""assert-stamped.sh — the read-only PRE-MERGE backstop over fork..HEAD.

It asserts every non-merge commit carries `Change: <id>` and that no
fixup!/squash!/amend! directive subject remains, and performs NO rebase/mutation.
  0 -> invariant holds
  1 -> an unstamped commit or a leftover fixup directive (pause for a human)
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

ASSERT = Path("src/imp/assets/workflows/imp/scripts/assert-stamped.sh").resolve()
ID = "2026-06-20-assert-test"


def _home(pr: Path) -> Path:
    return pr.parent / ".imp" / "changes" / "x"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True,
                          check=False, env={**os.environ, "GIT_EDITOR": "true"})


def _run(pr: Path, change_id: str = ID) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ASSERT)], cwd=pr.parent,
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


def _commit(pr: Path, name: str, body: str, msg: str) -> None:
    (pr / name).write_text(body)
    _git(pr, "add", name)
    _git(pr, "commit", "-m", msg)


def test_passes_when_all_stamped_and_no_directives(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    _commit(pr, "a.txt", "a\n", f"feat a\n\nChange: {ID}")
    _commit(pr, "b.txt", "b\n", f"feat b\n\nChange: {ID}")
    res = _run(pr)
    assert res.returncode == 0, res.stderr


def test_fails_on_unstamped_commit(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    _commit(pr, "a.txt", "a\n", f"feat a\n\nChange: {ID}")
    _commit(pr, "b.txt", "b\n", "feat b")                 # no trailer
    res = _run(pr)
    assert res.returncode == 1, res.stderr
    assert "missing Change" in res.stderr


def test_fails_on_leftover_fixup_directive(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    _commit(pr, "a.txt", "a\n", f"feat a\n\nChange: {ID}")
    target = _git(pr, "rev-parse", "HEAD").stdout.strip()
    (pr / "a.txt").write_text("a2\n")
    _git(pr, "add", "a.txt")
    _git(pr, "commit", f"--fixup={target}")               # unfolded fixup! subject remains
    res = _run(pr)
    assert res.returncode == 1, res.stderr
    assert "fixup" in res.stderr.lower()


def test_makes_no_commit(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    _commit(pr, "a.txt", "a\n", f"feat a\n\nChange: {ID}")
    head_before = _git(pr, "rev-parse", "HEAD").stdout.strip()
    res = _run(pr)
    assert res.returncode == 0, res.stderr
    assert _git(pr, "rev-parse", "HEAD").stdout.strip() == head_before   # read-only


def test_empty_change_id_fails(tmp_path: Path) -> None:
    pr = tmp_path / "wt"
    _init(pr)
    _record_change(pr)
    _git(pr, "checkout", "-b", "imp/x")
    _commit(pr, "a.txt", "a\n", f"feat a\n\nChange: {ID}")
    res = _run(pr, change_id="")
    assert res.returncode != 0                            # ${IMP_CHANGE_ID:?}
