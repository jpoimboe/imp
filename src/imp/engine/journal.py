"""Read-only helpers over the refs/imp/journal orphan ref (spec §4, §6).

The journal_commit WRITER lives in worktree.py; this module is the READ side.
Neither helper ever checks out the ref — both shell out to git plumbing via
the single binary helper _git_bytes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

_JOURNAL_REF = "refs/imp/journal"


def _git_bytes(cwd: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True)


def journal_ids(control_root: Path) -> list[str]:
    """Top-level entries of refs/imp/journal (each is a Change id). Returns []
    when the ref is absent or control_root is not a git repo — never raises."""
    proc = _git_bytes(control_root, "ls-tree", "--name-only", _JOURNAL_REF)
    if proc.returncode != 0:
        return []
    return [
        line.strip()
        for line in proc.stdout.decode("utf-8", errors="replace").splitlines()
        if line.strip()
    ]


def journal_show(control_root: Path, change_id: str, name: str) -> bytes | None:
    """Bytes of refs/imp/journal:<id>/<name>, or None if absent."""
    proc = _git_bytes(control_root, "cat-file", "-p", f"{_JOURNAL_REF}:{change_id}/{name}")
    if proc.returncode != 0:
        return None
    return proc.stdout
