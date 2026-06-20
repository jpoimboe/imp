"""One-time importer from the legacy refs/imp/journal ref into the archive."""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path

from imp.engine import archive, journal, worktree

_RUNTIME_FILES = ("events.jsonl", "change.json", "state.json")


def import_change_runtime(control_root: Path, change_id: str) -> bool:
    """Reconstruct one journaled Change's record + events blob + index rows from
    refs/imp/journal:<id>. Returns False when the Change's change.json/state.json
    are absent from the journal (nothing to import)."""
    change_json = journal.journal_show(control_root, change_id, "change.json")
    state_json = journal.journal_show(control_root, change_id, "state.json")
    if change_json is None or state_json is None:
        return False
    events = journal.journal_show(control_root, change_id, "events.jsonl") or b""
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        (tmp / "events.jsonl").write_bytes(events)
        (tmp / "change.json").write_bytes(change_json)
        (tmp / "state.json").write_bytes(state_json)
        archive.archive_change(
            control_root, change_id,
            events_path=tmp / "events.jsonl",
            change_json_path=tmp / "change.json",
            state_json_path=tmp / "state.json")
    return True


def _wip_ref_exists(control_root: Path, change_id: str) -> bool:
    proc = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet",
         f"refs/imp/journal-wip/{change_id}"],
        cwd=control_root, capture_output=True, text=True)
    return proc.returncode == 0


def _journal_artifact_rels(control_root: Path, change_id: str) -> list[str]:
    """Recursive non-runtime paths under refs/imp/journal:<id> (e.g.
    spec/<id>.md, plans/<id>-plan-1.md), relative to the Change's subtree."""
    proc = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", f"refs/imp/journal:{change_id}"],
        cwd=control_root, capture_output=True, text=True)
    if proc.returncode != 0:
        return []
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and line.strip() not in _RUNTIME_FILES
    ]


def import_change_artifacts(control_root: Path, change_id: str) -> bool:
    """Materialize refs/imp/journal-wip/<id> from the journal's non-runtime
    subtrees. Skips (returns False) when the ref already exists (a recent Change
    already carries its artifacts there) or when the journal has no artifacts."""
    if _wip_ref_exists(control_root, change_id):
        return False
    rels = _journal_artifact_rels(control_root, change_id)
    if not rels:
        return False
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        files: dict[str, Path] = {}
        for rel in rels:
            blob = journal.journal_show(control_root, change_id, rel)
            if blob is None:
                continue
            dest = tmp / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(blob)
            files[rel] = dest
        if not files:
            return False
        worktree.artifact_commit(
            control_root, change_id, files, message=f"migrate: {change_id}")
    return True


def import_journal(control_root: Path) -> list[str]:
    """Import every Change in refs/imp/journal into the archive (record + events
    blob + index rows) and materialize each one's artifact ref. Idempotent.
    Returns the ids that were imported."""
    imported: list[str] = []
    for change_id in journal.journal_ids(control_root):
        if import_change_runtime(control_root, change_id):
            import_change_artifacts(control_root, change_id)
            imported.append(change_id)
    return imported
