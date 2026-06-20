"""Per-Change archive: content-addressed blobs + SQLite index + record files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

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
) -> None:
    """Finalize one completed/abandoned Change into the archive. Idempotent."""
    events_bytes = events_path.read_bytes() if events_path.exists() else b""
    store = LocalBlobStore(blobs_root(control_root))
    events_hash = store.put(events_bytes)

    change_meta = json.loads(change_json_path.read_text())
    final_state = json.loads(state_json_path.read_text())
    rec = record.build_record(change_meta, final_state, events_hash)
    record.write_record(records_root(control_root), rec)

    conn = index.connect(index_path(control_root))
    try:
        index.ingest(conn, rec, _parse_events(events_bytes))
    finally:
        conn.close()


def rebuild_index(control_root: Path) -> None:
    """Reconstruct index.db from record files + locally present blobs."""
    index_path(control_root).unlink(missing_ok=True)
    store = LocalBlobStore(blobs_root(control_root))
    conn = index.connect(index_path(control_root))
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
