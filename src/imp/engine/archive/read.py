"""Read side of the per-Change archive: index/record/blob store."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

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
    return sorted(_read_index_ids(control_root))


def is_archived(control_root: Path, change_id: str) -> bool:
    db = index_path(control_root)
    if not db.exists():
        return False
    conn = index.connect(db)
    try:
        return index.has_change(conn, change_id)
    finally:
        conn.close()


def completed_rows(
    control_root: Path,
) -> list[tuple[str, str, str, str, str, str, str | None]]:
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
    if rec is None:
        return None
    return LocalBlobStore(blobs_root(control_root)).get(rec["events_hash"])


def read_state_json(control_root: Path, change_id: str) -> bytes | None:
    rec = record.read_record(records_root(control_root), change_id)
    if rec is None:
        return None
    return json.dumps(rec["final_state"]).encode("utf-8")


def read_change_json(control_root: Path, change_id: str) -> bytes | None:
    rec = record.read_record(records_root(control_root), change_id)
    if rec is None:
        return None
    meta = rec.get("change_meta")
    if meta is not None:
        return json.dumps(meta).encode("utf-8")
    return json.dumps({
        "id": rec["id"], "title": rec["title"], "base_branch": rec["base"],
        "created_at": rec["created_at"], "depends_on": rec["depends_on"],
        "materialized": True,
    }).encode("utf-8")


def _git_bytes(control_root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(["git", *args], cwd=control_root, capture_output=True)


def read_artifact(control_root: Path, change_id: str, rel: str) -> bytes | None:
    wip = _git_bytes(
        control_root, "cat-file", "-p", f"refs/imp/journal-wip/{change_id}:{rel}")
    if wip.returncode == 0:
        return wip.stdout
    return None


def pick_newest_plan(change_id: str, names: list[str]) -> str | None:
    best, pick = -1, None
    for name in names:
        mobj = re.fullmatch(rf"{re.escape(change_id)}-plan-(\d+)\.md", name)
        if mobj and int(mobj.group(1)) > best:
            best, pick = int(mobj.group(1)), name
    return pick


def newest_plan_name(control_root: Path, change_id: str) -> str | None:
    """Highest-N plan filename from the retained wip ref; bare filename."""
    treeish = f"refs/imp/journal-wip/{change_id}:plans"
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


def rehydrate_change(control_root: Path, change_id: str) -> Path:
    """Recreate a live scratch dir from the archive so a left conversation can be resumed."""
    change_dir = control_root / ".imp" / "changes" / change_id
    if change_dir.exists():
        raise FileExistsError(f"live home for {change_id!r} already exists")
    change_json = read_change_json(control_root, change_id)
    if change_json is None:
        raise FileNotFoundError(f"no archived record for {change_id!r}")
    state_json = read_state_json(control_root, change_id)
    events = read_events(control_root, change_id)
    (change_dir / "state").mkdir(parents=True)
    (change_dir / "change.json").write_bytes(change_json)
    (change_dir / "state" / "state.json").write_bytes(state_json or b"{}")
    (change_dir / "events.jsonl").write_bytes(events or b"")
    return change_dir


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
