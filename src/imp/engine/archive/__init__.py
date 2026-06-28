"""Per-Change archive: content-addressed blobs + SQLite index + record files."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import filelock

from imp.engine.archive import index, record
from imp.engine.archive.blobstore import LocalBlobStore


def archive_root(control_root: Path) -> Path:
    return control_root / ".imp" / "archive"


def blobs_root(control_root: Path) -> Path:
    return archive_root(control_root) / "blobs"


def records_root(control_root: Path) -> Path:
    return archive_root(control_root) / "records"


def index_path(control_root: Path) -> Path:
    return archive_root(control_root) / "index.db"


def _parse_events(raw: bytes) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"_malformed": True, "raw": line})
    return out


def archive_change(
    control_root: Path, change_id: str, *,
    events_path: Path, change_json_path: Path, state_json_path: Path,
    steps_dir: Path | None = None,
    resumable: bool = False,
) -> None:
    """Finalize one completed/abandoned/left Change into the archive. Idempotent."""
    events_bytes = events_path.read_bytes() if events_path.exists() else b""
    store = LocalBlobStore(blobs_root(control_root))
    events_hash = store.put(events_bytes)

    step_blobs: dict[str, str] = {}
    if resumable and steps_dir is not None and steps_dir.is_dir():
        for path in sorted(steps_dir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(steps_dir).as_posix()
                step_blobs[rel] = store.put(path.read_bytes())

    change_meta = json.loads(change_json_path.read_text())
    final_state = json.loads(state_json_path.read_text())
    rec = record.build_record(
        change_meta, final_state, events_hash,
        resumable=resumable, step_blobs=step_blobs)
    record.write_record(records_root(control_root), rec)

    conn = index.connect(index_path(control_root))
    try:
        index.ingest(conn, rec, _parse_events(events_bytes))
    finally:
        conn.close()


def rebuild_index(control_root: Path) -> None:
    """Reconstruct index.db from record files + locally present blobs.

    Builds a temp db in the archive dir and os.replaces it into position, so a
    concurrent reader never observes a missing or half-built index.
    """
    archive_root(control_root).mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=archive_root(control_root), suffix=".db.tmp")
    os.close(fd)
    try:
        store = LocalBlobStore(blobs_root(control_root))
        conn = index.connect(Path(tmp))
        try:
            for cid in record.list_record_ids(records_root(control_root)):
                rec = record.read_record(records_root(control_root), cid)
                if rec is None:
                    continue
                blob = store.get(rec["events_hash"])
                events = _parse_events(blob) if blob is not None else []
                index.ingest(conn, rec, events)
        finally:
            conn.close()
        os.replace(tmp, index_path(control_root))
    finally:
        Path(tmp).unlink(missing_ok=True)


def _read_user_version(control_root: Path) -> int:
    db = index_path(control_root)
    if not db.exists():
        # Absent db: nothing to migrate; connect() stamps a fresh db current.
        return index._SCHEMA_VERSION
    conn = sqlite3.connect(db, timeout=30)
    try:
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    finally:
        conn.close()


def ensure_current(control_root: Path) -> None:
    """Rebuild the index if its schema version is stale.

    Double-checked locking: a lock-free version read fast-paths the steady state;
    the one-time migration rebuild runs under the writer lock with a re-check so
    two processes never rebuild twice.
    """
    if _read_user_version(control_root) >= index._SCHEMA_VERSION:
        return
    with filelock.FileLock(str(index_path(control_root)) + ".filelock"):
        if _read_user_version(control_root) >= index._SCHEMA_VERSION:
            return
        rebuild_index(control_root)
