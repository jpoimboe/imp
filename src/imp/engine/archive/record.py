"""Immutable per-Quest record file: build, write, and read."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from imp.engine.pagecache import drop_page_cache


def build_record(
    quest_meta: dict[str, Any], final_state: dict[str, Any], events_hash: str,
    *, resumable: bool = False, step_blobs: dict[str, str] | None = None,
) -> dict[str, Any]:
    return {
        "id": quest_meta["id"],
        "title": quest_meta["title"],
        "base": quest_meta["base_branch"],
        "created_at": quest_meta.get("created_at", ""),
        "depends_on": list(quest_meta.get("depends_on") or []),
        "quest_meta": dict(quest_meta),
        "resumable": resumable,
        "final_state": final_state,
        "events_hash": events_hash,
        "step_blobs": dict(step_blobs or {}),
    }


def write_record(records_dir: Path, record: dict[str, Any]) -> Path:
    """Atomic write (tmp -> fsync -> os.replace), mirroring State.save: the
    record is the archive's source of truth with no git recovery, and readers
    (status fallback, imp log, rebuild_index) race this write — they must only
    ever see a complete record, never a truncate-then-write torn state."""
    records_dir.mkdir(parents=True, exist_ok=True)
    path = records_dir / f"{record['id']}.json"
    fd, tmp_name = tempfile.mkstemp(prefix=".record-", dir=records_dir)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(record, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    return path


def _read_record_bytes(path: Path) -> bytes:
    """Single seam for every record read (initial + retries) so the transient
    recovery path is exercisable in tests. Sibling: state._read_state_bytes."""
    return path.read_bytes()


# Same rationale as state._STATE_READ_RETRIES: the write path is atomic, so a
# parse failure on a file that is valid on disk is a transient read-path fault
# (btrfs+zstd head-drop); a cold re-read recovers. If every retry fails the
# bytes are genuinely bad — the original error is raised and the record file
# itself remains on disk as the forensic evidence (records are written once,
# never overwritten by a later run).
_RECORD_READ_RETRIES = 4


def _reread_or_raise(
        path: Path, err: json.JSONDecodeError | UnicodeDecodeError) -> dict[str, Any]:
    for attempt in range(1, _RECORD_READ_RETRIES + 1):
        time.sleep(0.002 * attempt)       # let the transient settle; rare path
        drop_page_cache(path)
        try:
            return cast(
                dict[str, Any],
                json.loads(_read_record_bytes(path).decode("utf-8")))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
    raise err


def read_record(records_dir: Path, quest_id: str) -> dict[str, Any] | None:
    path = records_dir / f"{quest_id}.json"
    if not path.exists():
        return None
    raw = _read_record_bytes(path)
    try:
        return cast(dict[str, Any], json.loads(raw.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        return _reread_or_raise(path, err)


def list_record_ids(records_dir: Path) -> list[str]:
    if not records_dir.is_dir():
        return []
    return sorted(p.stem for p in records_dir.glob("*.json"))
