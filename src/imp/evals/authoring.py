"""Local authoring aid: split a hand-picked, contiguous commit range into TaskSlices
+ the matching cumulative diff, so multi-task fixtures are consistent-by-construction
rather than validated-after-the-fact. NOT mining - it operates on an ordered commit
list the author chooses, not session history."""

from __future__ import annotations

import subprocess
from pathlib import Path

from imp.evals.models import TaskSlice


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True
    ).stdout


def split_commits_into_tasks(
    repo: Path, commit_shas: list[str]
) -> tuple[list[TaskSlice], str, str, str]:
    """Return (tasks, base_sha, head_sha, cumulative_diff) for an ORDERED list of
    contiguous commits. base = parent of the first commit; head = the last commit.
    Each TaskSlice.diff is `git diff <commit>~..<commit>` and its task_context is the
    commit subject. cumulative_diff is `git diff base..head`. Apply with the SAME
    `git apply --whitespace=nowarn` the harness uses so the tree-hash invariant holds."""
    if not commit_shas:
        raise ValueError("need at least one commit to split into tasks")
    base = _git(repo, "rev-parse", f"{commit_shas[0]}~1").strip()
    head = _git(repo, "rev-parse", commit_shas[-1]).strip()
    tasks: list[TaskSlice] = []
    for i, sha in enumerate(commit_shas):
        diff = _git(repo, "diff", f"{sha}~1..{sha}")
        subject = _git(repo, "log", "-1", "--format=%s", sha).strip()
        tasks.append(TaskSlice(index=i, diff=diff, task_context=subject))
    cumulative = _git(repo, "diff", f"{base}..{head}")
    return tasks, base, head, cumulative
