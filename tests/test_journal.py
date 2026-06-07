"""Read-side helpers over refs/imp/journal (src/imp/engine/journal.py).

The journal_commit WRITER lives in worktree.py; these tests cover the READ
helpers, which shell out to git plumbing and never check out the ref.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from imp.engine import journal
from imp.engine import worktree as wt


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=False,
    )


def _init_repo(root: Path) -> None:
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@t")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text(".imp/\n")
    (root / "README.md").write_text("seed\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")


def test_journal_ids_empty_when_ref_absent(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    assert journal.journal_ids(tmp_path) == []


def test_journal_ids_not_a_repo_returns_empty(tmp_path: Path) -> None:
    # No git repo at all: must degrade to [] rather than raising.
    assert journal.journal_ids(tmp_path) == []


def test_journal_ids_and_show_roundtrip(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    stage = tmp_path / ".imp"
    stage.mkdir(exist_ok=True)
    ev = stage / "events.jsonl"
    ev.write_text('{"type":"change_created"}\n')
    cj = stage / "change.json"
    cj.write_text('{"slug":"alpha","title":"Alpha"}')
    wt.journal_commit(tmp_path, "alpha", {"events.jsonl": ev, "change.json": cj})

    assert journal.journal_ids(tmp_path) == ["alpha"]
    assert journal.journal_show(tmp_path, "alpha", "events.jsonl") == b'{"type":"change_created"}\n'
    assert (
        journal.journal_show(tmp_path, "alpha", "change.json")
        == b'{"slug":"alpha","title":"Alpha"}'
    )


def test_journal_show_missing_returns_none(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    stage = tmp_path / ".imp"
    stage.mkdir(exist_ok=True)
    ev = stage / "events.jsonl"
    ev.write_text("{}\n")
    wt.journal_commit(tmp_path, "alpha", {"events.jsonl": ev})

    assert journal.journal_show(tmp_path, "alpha", "state.json") is None
    assert journal.journal_show(tmp_path, "nope", "events.jsonl") is None
