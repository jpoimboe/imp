from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from imp.engine import archive
from imp.engine.archive import record
from imp.engine.archive.blobstore import LocalBlobStore, hash_content
from imp.engine.resume_state import encode_native_resume_state
from imp.providers.codex_rollout import CODEX_ROLLOUT_SLOTS, make_codex_resume_state

_PREFIX = "build/coder"
_RESTORE_PATH = "sessions/2026/09/rollout-1.jsonl"
_ROLLOUT = b'{"type":"session_meta","payload":{"id":"thread-1"}}\n'


def _record(cid: str, step_blobs: object) -> dict:
    rec = record.build_record(
        {
            "id": cid,
            "title": "Archived",
            "base_branch": "main",
            "created_at": "2026-09-01T00:00:00+00:00",
            "depends_on": [],
            "workflow": "default",
            "provider": "codex",
            "materialized": False,
        },
        {
            "id": cid,
            "status": "shelved",
            "step": "coder",
            "started_at": "2026-09-01T00:00:01+00:00",
        },
        hash_content(b""),
        resumable=True,
        step_blobs={},
    )
    rec["step_blobs"] = step_blobs
    return rec


def _write_record(control: Path, rec: dict) -> None:
    record.write_record(archive.records_root(control), rec)


def _seed_v11(
    control: Path,
    cid: str = "legacy",
    *,
    owner: bytes | None = None,
    metadata: bytes | None = None,
    rollout: bytes = _ROLLOUT,
) -> dict:
    store = LocalBlobStore(archive.blobs_root(control))
    store.put(b"")
    if metadata is None:
        metadata = json.dumps(
            {"thread_id": "thread-1", "rollout_relpath": _RESTORE_PATH},
            separators=(",", ":"),
        ).encode()
    step_blobs = {
        f"{_PREFIX}/codex_thread.json": store.put(metadata),
        f"{_PREFIX}/codex_rollout/{_RESTORE_PATH}": store.put(rollout),
    }
    if owner is not None:
        step_blobs[f"{_PREFIX}/provider"] = store.put(owner)
    rec = _record(cid, step_blobs)
    _write_record(control, rec)
    archive.data_version_path(control).parent.mkdir(parents=True, exist_ok=True)
    archive.data_version_path(control).write_text("11")
    return rec


def _seed_current_v11(control: Path, cid: str = "current") -> dict:
    store = LocalBlobStore(archive.blobs_root(control))
    store.put(b"")
    state = make_codex_resume_state(
        thread_id="thread-current",
        restore_path="sessions/current.jsonl",
        rollout_slot=CODEX_ROLLOUT_SLOTS[1],
    )
    step_blobs = {
        f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}": store.put(b"SLOT-A\n"),
        f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[1]}": store.put(b"SLOT-B\n"),
        f"{_PREFIX}/resume-state.json": store.put(encode_native_resume_state(state)),
        f"{_PREFIX}/provider": store.put(b"codex\n"),
    }
    rec = _record(cid, step_blobs)
    _write_record(control, rec)
    archive.data_version_path(control).parent.mkdir(parents=True, exist_ok=True)
    archive.data_version_path(control).write_text("11")
    return rec


def _blob_snapshot(control: Path) -> dict[str, bytes]:
    root = archive.blobs_root(control)
    if not root.exists():
        return {}
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _archive_snapshot(control: Path) -> dict[str, bytes]:
    root = archive.archive_root(control)
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and path.name != "index.db"
        and not path.name.endswith((".filelock", ".tmp"))
    }


def _index_snapshot(control: Path) -> tuple[int, list[tuple], list[tuple]]:
    conn = sqlite3.connect(archive.index_path(control))
    try:
        version = int(conn.execute("PRAGMA user_version").fetchone()[0])
        quests = conn.execute(
            "SELECT id,title,base,created_at,status,step,events_hash,"
            "last_event_ts,content,events_content,ingested_at IS NOT NULL "
            "FROM quests ORDER BY id"
        ).fetchall()
        deps = conn.execute(
            "SELECT quest_id,depends_on_id FROM deps ORDER BY quest_id,depends_on_id"
        ).fetchall()
    finally:
        conn.close()
    return version, quests, deps


def _plan_blob(plan, path: str, before: dict[str, bytes]) -> bytes:
    digest = plan.step_blobs[path]
    existing = before.get(f"{digest[:2]}/{digest[2:]}")
    if existing is not None:
        return existing
    return next(blob for blob in plan.blob_bytes if hash_content(blob) == digest)
