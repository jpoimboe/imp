"""Read side of the per-Change archive: unions index/record/blob with the legacy journal."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from imp.engine import journal
from imp.engine.archive import blobs_root, index, index_path, record, records_root
from imp.engine.archive.blobstore import LocalBlobStore


def _read_index_ids(control_root: Path) -> list[str]:
    db = index_path(control_root)
    if not db.exists():
        return []
    conn = index.connect(db)
    try:
        return index.change_ids(conn)
    finally:
        conn.close()


def archived_ids(control_root: Path) -> list[str]:
    ids = set(_read_index_ids(control_root))
    ids.update(journal.journal_ids(control_root))
    return sorted(ids)


def is_archived(control_root: Path, change_id: str) -> bool:
    db = index_path(control_root)
    if db.exists():
        conn = index.connect(db)
        try:
            if index.has_change(conn, change_id):
                return True
        finally:
            conn.close()
    return change_id in journal.journal_ids(control_root)


def completed_rows(
    control_root: Path,
) -> list[tuple[str, str, str, str, str, str]]:
    db = index_path(control_root)
    if not db.exists():
        return []
    conn = index.connect(db)
    try:
        return index.completed_rows(conn)
    finally:
        conn.close()


def read_events(control_root: Path, change_id: str) -> bytes | None:
    rec = record.read_record(records_root(control_root), change_id)
    if rec is not None:
        blob = LocalBlobStore(blobs_root(control_root)).get(rec["events_hash"])
        if blob is not None:
            return blob
    return journal.journal_show(control_root, change_id, "events.jsonl")


def read_state_json(control_root: Path, change_id: str) -> bytes | None:
    rec = record.read_record(records_root(control_root), change_id)
    if rec is not None:
        return json.dumps(rec["final_state"]).encode("utf-8")
    return journal.journal_show(control_root, change_id, "state.json")


def read_change_json(control_root: Path, change_id: str) -> bytes | None:
    rec = record.read_record(records_root(control_root), change_id)
    if rec is not None:
        return json.dumps({
            "id": rec["id"], "title": rec["title"], "base_branch": rec["base"],
            "created_at": rec["created_at"], "depends_on": rec["depends_on"],
            "materialized": True,
        }).encode("utf-8")
    return journal.journal_show(control_root, change_id, "change.json")


def _git_bytes(control_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=control_root, capture_output=True)


def read_artifact(control_root: Path, change_id: str, rel: str) -> bytes | None:
    wip = _git_bytes(
        control_root, "cat-file", "-p", f"refs/imp/journal-wip/{change_id}:{rel}")
    if wip.returncode == 0:
        return wip.stdout
    return journal.journal_show(control_root, change_id, rel)


def pick_newest_plan(change_id: str, names: list[str]) -> str | None:
    best, pick = -1, None
    for name in names:
        mobj = re.fullmatch(rf"{re.escape(change_id)}-plan-(\d+)\.md", name)
        if mobj and int(mobj.group(1)) > best:
            best, pick = int(mobj.group(1)), name
    return pick


def newest_plan_name(control_root: Path, change_id: str) -> str | None:
    """Highest-N plan filename from the wip ref, else the legacy journal; bare filename."""
    for treeish in (f"refs/imp/journal-wip/{change_id}:plans",
                    f"refs/imp/journal:{change_id}/plans"):
        proc = subprocess.run(
            ["git", "ls-tree", "--name-only", treeish],
            cwd=control_root, capture_output=True, text=True)
        if proc.returncode == 0:
            pick = pick_newest_plan(
                change_id,
                [ln.strip() for ln in proc.stdout.splitlines() if ln.strip()])
            if pick:
                return pick
    return None


def last_event_iso(control_root: Path, change_id: str) -> str | None:
    raw = read_events(control_root, change_id)
    if raw is None:
        return None
    stripped = raw.rstrip()
    if not stripped:
        return None
    line = stripped[stripped.rfind(b"\n") + 1:].decode("utf-8", errors="replace")
    try:
        return str(json.loads(line)["timestamp"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def status_of(control_root: Path, change_id: str) -> str | None:
    raw = read_state_json(control_root, change_id)
    if raw is None:
        return None
    try:
        return str(json.loads(raw)["status"])
    except (json.JSONDecodeError, KeyError, TypeError):
        return None
