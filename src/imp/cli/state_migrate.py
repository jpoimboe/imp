"""One-shot startup migration of a project's runtime state out of the checkout and
into its state directory. The only reader of the pre-migration layout left."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def legacy_state_dir(control_root: Path) -> Path:
    """Where per-project state lived before this migration."""
    return control_root / ".imp"


def _same_device(a: Path, b: Path) -> bool:
    """Module-level so the cross-device branch is reachable on a single-fs tmpdir."""
    return a.stat().st_dev == b.stat().st_dev


def _discard(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


def _move_entries(legacy: Path, state_dir: Path) -> None:
    """Relocate each top-level entry, resumably: a destination that already exists
    was moved by an earlier run, and the source is never touched until the
    destination is in place — so a partial staging dir is always safe to discard."""
    same = _same_device(legacy, state_dir)
    for entry in sorted(legacy.iterdir()):
        dest = state_dir / entry.name
        if dest.exists():
            _discard(entry)
            continue
        if same:
            os.rename(entry, dest)
            continue
        staging = state_dir / f".migrating-{entry.name}"
        _discard(staging)
        if entry.is_dir() and not entry.is_symlink():
            shutil.copytree(entry, staging, symlinks=True)
        else:
            shutil.copy2(entry, staging, follow_symlinks=False)
        os.rename(staging, dest)
        _discard(entry)


def _prefixes(legacy: Path, state_dir: Path, name: str) -> tuple[str, str]:
    return f"{legacy / name}{os.sep}", f"{state_dir / name}{os.sep}"


def _rewrite_quest_roots(legacy: Path, state_dir: Path) -> None:
    """A draft's project_root is the control root, which carries no worktree
    prefix — matching on it is what leaves drafts intact."""
    from imp.engine.quest import _write_quest_json

    old, new = _prefixes(legacy, state_dir, "worktrees")
    quests = state_dir / "quests"
    if not quests.is_dir():
        return
    for meta_path in sorted(quests.glob("*/quest.json")):
        meta = json.loads(meta_path.read_text())
        root = str(meta.get("project_root") or "")
        if not root.startswith(old):
            continue
        meta["project_root"] = new + root[len(old):]
        _write_quest_json(meta_path, meta)


def _rewrite_archive_roots(legacy: Path, state_dir: Path) -> None:
    from imp.engine.archive.record import write_record

    old, new = _prefixes(legacy, state_dir, "worktrees")
    records = state_dir / "archive" / "records"
    if not records.is_dir():
        return
    for record_path in sorted(records.glob("*.json")):
        record = json.loads(record_path.read_text())
        root = str(record.get("quest_meta", {}).get("project_root") or "")
        if not root.startswith(old):
            continue
        record["quest_meta"]["project_root"] = new + root[len(old):]
        write_record(records, record)


def _registered_worktrees(control_root: Path) -> list[str]:
    """The paths git holds admin records for. A worktree whose directory moved is
    still listed at its OLD path (marked prunable) — which is what makes this the
    right source for the fixup — while a pruned or never-registered directory is
    not listed at all."""
    proc = subprocess.run(
        ["git", "-C", str(control_root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=True)
    return [line.removeprefix("worktree ") for line in proc.stdout.splitlines()
            if line.startswith("worktree ")]


def _repair_worktrees(control_root: Path, legacy: Path, state_dir: Path) -> None:
    """`git worktree repair` exits non-zero on any path git has no record of, and
    remove_worktree's and destroy_worktree's force paths leave such directories
    behind. Taking the set from git makes that unrepresentable; a refusal on a
    registered worktree is reported rather than raised, because the state has
    already moved by now and the legacy directory is removed only at the very end —
    so raising would wedge every later invocation on the same failure."""
    old, new = _prefixes(legacy, state_dir, "worktrees")
    moved = [new + path[len(old):] for path in _registered_worktrees(control_root)
             if path.startswith(old)]
    if not moved:
        return
    proc = subprocess.run(
        ["git", "-C", str(control_root), "worktree", "repair", *moved],
        capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        print(f"imp: git could not repair every moved worktree:\n{proc.stderr.strip()}",
              file=sys.stderr)


def _relink_quest_homes(legacy: Path, state_dir: Path) -> None:
    """Re-point by the link's own target, not by the worktree's directory name: an
    isolated subagent's spike worktree carries the same link under a
    `spike-<id>-<n>` name that is not a quest id."""
    old, new = _prefixes(legacy, state_dir, "quests")
    worktrees = state_dir / "worktrees"
    if not worktrees.is_dir():
        return
    for link in sorted(worktrees.glob("*/.imp/quests/*")):
        target = os.readlink(link) if link.is_symlink() else ""
        if not target.startswith(old):
            continue
        link.unlink()
        link.symlink_to(new + target[len(old):])
