# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Per-quest archive: content-addressed blobs + SQLite index + record files."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import sys
import tempfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import filelock

from imp.engine import durable, worktree
from imp.engine.archive import index, record
from imp.engine.archive.blobstore import LocalBlobStore
from imp.engine.errors import ArchiveMismatch
from imp.paths import project_state_dir


def archive_root(control_root: Path) -> Path:
    return project_state_dir(control_root) / "archive"


def blobs_root(control_root: Path) -> Path:
    return archive_root(control_root) / "blobs"


def records_root(control_root: Path) -> Path:
    return archive_root(control_root) / "records"


def index_path(control_root: Path) -> Path:
    return archive_root(control_root) / "index.db"


def data_version_path(control_root: Path) -> Path:
    return archive_root(control_root) / "data_version"


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


def archive_quest(
    control_root: Path, quest_id: str, *,
    events_path: Path, quest_json_path: Path, state_json_path: Path,
    steps_dir: Path | None = None,
    resumable: bool = False,
) -> None:
    """Finalize one completed/abandoned/archived quest into the archive. Idempotent."""
    check_data_floor(control_root)
    ensure_current(control_root)
    marker = data_version_path(control_root)
    archive_root(control_root).mkdir(parents=True, exist_ok=True)
    with filelock.FileLock(str(index_path(control_root)) + ".filelock"):
        if not marker.exists():
            _write_data_version(control_root, _DATA_VERSION)

    events_bytes = events_path.read_bytes() if events_path.exists() else b""
    store = LocalBlobStore(blobs_root(control_root))
    events_hash = store.put(events_bytes)

    step_blobs: dict[str, str] = {}
    if resumable and steps_dir is not None and steps_dir.is_dir():
        for path in sorted(steps_dir.rglob("*")):
            if path.is_file():
                rel = path.relative_to(steps_dir).as_posix()
                step_blobs[rel] = store.put(path.read_bytes())

    quest_meta = json.loads(quest_json_path.read_text())
    final_state = json.loads(state_json_path.read_text())
    rec = record.build_record(
        quest_meta, final_state, events_hash,
        resumable=resumable, step_blobs=step_blobs)
    record.write_record(records_root(control_root), rec)

    with filelock.FileLock(str(index_path(control_root)) + ".filelock"):
        conn = index.connect(index_path(control_root))
        try:
            index.ingest(conn, rec, _parse_events(events_bytes))
            index.stamp_version(conn)
        finally:
            conn.close()
        _write_data_version(control_root, _DATA_VERSION)


@contextlib.contextmanager
def _progress(label: str) -> Iterator[Callable[[int, int], None]]:
    stream = sys.stderr
    tty = stream.isatty()
    painted = False

    def emit(i: int, total: int) -> None:
        nonlocal painted
        if not tty:
            return
        stream.write(f"\r\x1b[K\x1b[2m{label} {i}/{total}\x1b[0m")
        stream.flush()
        painted = True

    try:
        yield emit
    finally:
        if painted:
            stream.write("\n")
            stream.flush()


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
            cids = list(record.list_record_ids(records_root(control_root)))
            total = len(cids)
            with _progress("Reindexing archive...") as emit:
                for i, cid in enumerate(cids, start=1):
                    emit(i, total)
                    rec = record.read_record(records_root(control_root), cid)
                    if rec is None:
                        continue
                    blob = store.get(rec["events_hash"])
                    events = _parse_events(blob) if blob is not None else []
                    index.ingest(conn, rec, events)
        finally:
            conn.close()
        with open(tmp, "rb") as stream:
            os.fsync(stream.fileno())
        os.replace(tmp, index_path(control_root))
        durable.fsync_directory(archive_root(control_root))
    finally:
        Path(tmp).unlink(missing_ok=True)


def _read_user_version(control_root: Path) -> int:
    db = index_path(control_root)
    if not db.exists():
        if record.list_record_ids(records_root(control_root)):
            return 0
        return index._SCHEMA_VERSION
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db, timeout=30)
        return int(conn.execute("PRAGMA user_version").fetchone()[0])
    except sqlite3.DatabaseError:
        return 0
    finally:
        if conn is not None:
            conn.close()


def _index_is_complete(control_root: Path) -> bool:
    record_ids = record.list_record_ids(records_root(control_root))
    db = index_path(control_root)
    if not db.exists():
        return not record_ids
    try:
        conn = sqlite3.connect(
            f"{db.resolve().as_uri()}?mode=ro", uri=True, timeout=30)
        try:
            conn.execute(
                "SELECT id, title, base, created_at, status, step, events_hash,"
                " ingested_at, last_event_ts, content, events_content"
                " FROM quests LIMIT 0")
            conn.execute(
                "SELECT quest_id, depends_on_id FROM deps LIMIT 0")
            indexed_ids = set(index.quest_ids(conn))
        finally:
            conn.close()
    except sqlite3.DatabaseError:
        return False
    return all(cid in indexed_ids for cid in record_ids)


# A blob migration changing an index-visible value must also bump index._SCHEMA_VERSION.
_DATA_VERSION = 14
_UPGRADABLE_FROM = 13   # the one stored version this install heals in place


def _read_data_version(control_root: Path) -> int:
    """Read the authoritative marker; only a truly empty archive is current."""
    marker = data_version_path(control_root)
    if marker.exists():
        try:
            return int(marker.read_text().strip())
        except (ValueError, OSError):
            return 0
    if index_path(control_root).exists() or record.list_record_ids(
            records_root(control_root)):
        return 0
    return _DATA_VERSION


def _write_data_version(control_root: Path, version: int) -> None:
    archive_root(control_root).mkdir(parents=True, exist_ok=True)
    durable.durable_replace_bytes(
        data_version_path(control_root), str(version).encode("ascii"))


def _serialize_events(events: list[dict[str, Any]]) -> bytes:
    """Compact one-JSON-object-per-line bytes — the on-disk events.jsonl shape."""
    return "".join(json.dumps(ev) + "\n" for ev in events).encode("utf-8")


def _commits_of(control_root: Path, event: dict[str, Any]) -> list[dict[str, str]]:
    """control_root IS the project's git checkout; the archive lives beside it."""
    if event.get("type") != "quest_merged":
        return []
    commit = str((event.get("payload") or {}).get("commit") or "")
    if not commit:
        return []
    return worktree.commits_in_range(control_root, f"{commit}^1..{commit}^2")


def _backfill_commits(
    control_root: Path, events: list[dict[str, Any]],
) -> bool:
    """Add `commits` to the terminal event; False when there is nothing to do."""
    for event in reversed(events):
        if event.get("type") not in ("quest_merged", "workflow_completed"):
            continue
        payload = event.get("payload")
        if not isinstance(payload, dict) or "commits" in payload:
            return False
        payload["commits"] = _commits_of(control_root, event)
        return True
    return False


def _upgrade_to_current(control_root: Path) -> None:
    """One-shot 13 -> 14: backfill `commits` onto each archived terminal event,
    repoint each record at its rewritten blob, rebuild the index so the derived
    text matches, then stamp the marker. Old blobs orphan harmlessly."""
    root = archive_root(control_root)
    if root.exists() and not os.access(root, os.W_OK):
        return
    with filelock.FileLock(str(index_path(control_root)) + ".filelock"):
        if _read_data_version(control_root) != _UPGRADABLE_FROM:
            return
        store = LocalBlobStore(blobs_root(control_root))
        records_dir = records_root(control_root)
        for cid in record.list_record_ids(records_dir):
            rec = record.read_record(records_dir, cid)
            if rec is None:
                continue
            blob = store.get(rec["events_hash"])
            if blob is None:
                continue
            events = _parse_events(blob)
            if not _backfill_commits(control_root, events):
                continue
            rec["events_hash"] = store.put(_serialize_events(events))
            record.write_record(records_dir, rec)
        rebuild_index(control_root)
        _write_data_version(control_root, _DATA_VERSION)


def check_data_floor(control_root: Path) -> None:
    """Gate any blob-backed read, write or delete on the archive's blob-schema floor.

    A stored version of exactly _UPGRADABLE_FROM heals in place first: this is the
    choke point every blob-backed operation passes, so no entry point can observe
    an un-migrated archive. Every other mismatch still refuses.
    """
    stored = _read_data_version(control_root)
    if stored == _UPGRADABLE_FROM:
        _upgrade_to_current(control_root)
        stored = _read_data_version(control_root)
    if stored != _DATA_VERSION:
        raise ArchiveMismatch(floor="data", stored=stored, known=_DATA_VERSION)


def check_index_floor(control_root: Path) -> None:
    """Refuse an index query whose schema floor is newer than this install knows."""
    stored = _read_user_version(control_root)
    if stored > index._SCHEMA_VERSION:
        raise ArchiveMismatch(
            floor="index", stored=stored, known=index._SCHEMA_VERSION)


def archive_is_current(archive_dir: Path) -> bool:
    """True iff this archive's blob layout is one this install reads. Addresses
    the directory as a path: another project's archive may be neither claimed
    nor rebuilt, so an absent marker reads as nothing to offer."""
    try:
        return int((archive_dir / "data_version").read_text().strip()) == _DATA_VERSION
    except (ValueError, OSError):
        return False


def ensure_current(control_root: Path) -> None:
    """Rebuild the index if its schema version is stale or it is missing a durable
    record.

    Double-checked locking: lock-free version reads fast-path the steady state;
    the one-time work runs under the writer lock with a re-check so two processes
    never do it twice.

    A read-only archive root is left exactly as it is: the stale index still
    answers every query.
    """
    root = archive_root(control_root)
    if root.exists() and not os.access(root, os.W_OK):
        return
    if _read_data_version(control_root) != _DATA_VERSION:
        return          # blobs this imp cannot vouch for: leave the archive as-is
    if (_read_user_version(control_root) >= index._SCHEMA_VERSION
            and _index_is_complete(control_root)):
        return
    with filelock.FileLock(str(index_path(control_root)) + ".filelock"):
        if (_read_user_version(control_root) >= index._SCHEMA_VERSION
                and _index_is_complete(control_root)):
            return
        rebuild_index(control_root)


def require_current(control_root: Path) -> None:
    check_data_floor(control_root)
    ensure_current(control_root)
