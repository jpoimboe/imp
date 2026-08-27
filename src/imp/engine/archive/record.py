# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Josh Poimboeuf
"""Immutable per-quest record file: build, write, and read."""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any, cast

from imp.engine import readfault
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


# Why a parse failure here is a read fault rather than corruption: imp.engine.readfault.
def _reread_or_raise(
        path: Path, raw: bytes,
        err: json.JSONDecodeError | UnicodeDecodeError) -> dict[str, Any]:
    """Re-read cold up to readfault.READ_RETRIES times, else record and re-raise."""
    for attempt in range(1, readfault.READ_RETRIES + 1):
        time.sleep(0.002 * attempt)       # let the transient settle; rare path
        drop_page_cache(path)
        try:
            retry_raw = _read_record_bytes(path)
            data: dict[str, Any] = json.loads(retry_raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError, OSError):
            continue
        readfault.record("archive_record", path, attempt=attempt,
                         reason=str(err), bad=raw, good=retry_raw)
        return data
    readfault.record("archive_record", path, attempt=None,
                     reason=str(err), bad=raw, good=None)
    raise err   # the bad record file stays put; records are written once, never rewritten


def read_record(records_dir: Path, quest_id: str) -> dict[str, Any] | None:
    path = records_dir / f"{quest_id}.json"
    if not path.exists():
        return None
    raw = _read_record_bytes(path)
    try:
        return cast(dict[str, Any], json.loads(raw.decode("utf-8")))
    except (json.JSONDecodeError, UnicodeDecodeError) as err:
        return _reread_or_raise(path, raw, err)


def list_record_ids(records_dir: Path) -> list[str]:
    if not records_dir.is_dir():
        return []
    return sorted(p.stem for p in records_dir.glob("*.json"))
