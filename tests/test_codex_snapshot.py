from __future__ import annotations

import subprocess
from pathlib import Path

from imp.providers.codex_runner import restore_to_snapshot, take_snapshot


def _run(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True,
                          capture_output=True, text=True).stdout


def _git_init(p: Path) -> None:
    _run(p, "init", "-q")
    _run(p, "config", "user.email", "t@t.t")
    _run(p, "config", "user.name", "T")


def _commit(p: Path, name: str, body: str) -> None:
    (p / name).write_text(body)
    _run(p, "add", name)
    _run(p, "commit", "-m", f"add {name}", "-q")


def test_snapshot_then_restore_undoes_tracked_modification(tmp_path: Path) -> None:
    _git_init(tmp_path)
    _commit(tmp_path, "a.txt", "ORIGINAL\n")
    snap = take_snapshot(tmp_path)
    (tmp_path / "a.txt").write_text("CORRUPTED\n")
    restore_to_snapshot(tmp_path, snap)
    assert (tmp_path / "a.txt").read_text() == "ORIGINAL\n"


def test_restore_removes_only_this_runs_untracked_keeps_preexisting(
    tmp_path: Path,
) -> None:
    _git_init(tmp_path)
    _commit(tmp_path, "a.txt", "ORIGINAL\n")
    (tmp_path / "prior.txt").write_text("KEEP ME\n")
    snap = take_snapshot(tmp_path)
    (tmp_path / "new.txt").write_text("NATIVE EDIT\n")
    restore_to_snapshot(tmp_path, snap)
    assert (tmp_path / "prior.txt").exists()
    assert (tmp_path / "prior.txt").read_text() == "KEEP ME\n"
    assert not (tmp_path / "new.txt").exists()


def test_restore_preserves_staged_preexisting_untracked(tmp_path: Path) -> None:
    """Pre-existing untracked file staged by Codex is NOT deleted by restore."""
    _git_init(tmp_path)
    _commit(tmp_path, "a.txt", "base\n")
    # Create a pre-existing untracked file
    untracked = tmp_path / "prior.txt"
    untracked.write_text("original content\n")
    snap = take_snapshot(tmp_path)
    assert "prior.txt" in snap.untracked
    # Simulate Codex staging it
    subprocess.run(["git", "add", "prior.txt"], cwd=str(tmp_path), check=True)
    # Restore should preserve the file
    restore_to_snapshot(tmp_path, snap)
    assert untracked.exists(), "pre-existing untracked file must survive restore"
    assert untracked.read_text() == "original content\n"


def test_empty_snapshot_means_reset_hard_HEAD(tmp_path: Path) -> None:
    """n-2: empty stash create output → reset --hard HEAD."""
    _git_init(tmp_path)
    _commit(tmp_path, "a.txt", "ORIGINAL\n")
    snap = take_snapshot(tmp_path)
    assert snap.stash_sha == ""  # clean worktree → empty stash output
    (tmp_path / "a.txt").write_text("CORRUPTED\n")
    (tmp_path / "extra.txt").write_text("NATIVE\n")
    restore_to_snapshot(tmp_path, snap)
    assert (tmp_path / "a.txt").read_text() == "ORIGINAL\n"
    assert not (tmp_path / "extra.txt").exists()
