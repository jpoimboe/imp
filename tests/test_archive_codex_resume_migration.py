from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from imp.engine import archive, durable
from imp.engine.archive import index, record
from imp.engine.archive.blobstore import LocalBlobStore, hash_content
from imp.engine.archive.codex_resume_migration import plan_archive_codex_resume_state
from imp.engine.resume_state import (
    InvalidResumeState,
    encode_native_resume_state,
    parse_native_resume_state,
)
from imp.providers.codex_rollout import (
    CODEX_ROLLOUT_SLOTS,
    CodexResumeState,
    make_codex_resume_state,
    parse_codex_resume_state,
)

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


def test_planner_converts_legacy_codex_state() -> None:
    legacy = json.dumps({
        "thread_id": "thread-1",
        "rollout_relpath": _RESTORE_PATH,
    }).encode()
    blobs = {
        hash_content(legacy): legacy,
        hash_content(_ROLLOUT): _ROLLOUT,
    }
    step_blobs = {
        f"{_PREFIX}/codex_thread.json": hash_content(legacy),
        f"{_PREFIX}/codex_rollout/{_RESTORE_PATH}": hash_content(_ROLLOUT),
    }

    plan = plan_archive_codex_resume_state(step_blobs, blobs.get)

    assert plan is not None
    state = parse_native_resume_state(_plan_blob(
        plan, f"{_PREFIX}/resume-state.json", {}
    ))
    assert (
        plan.step_blobs[f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}"],
        parse_codex_resume_state(state),
        plan.step_blobs[f"{_PREFIX}/provider"],
    ) == (
        hash_content(_ROLLOUT),
        CodexResumeState(
            thread_id="thread-1",
            restore_path=_RESTORE_PATH,
            rollout_slot=CODEX_ROLLOUT_SLOTS[0],
        ),
        hash_content(b"codex"),
    )


@pytest.mark.parametrize("owner", [None, b"codex\n"])
def test_migrates_exact_legacy_provenance_and_reruns_stably(
    tmp_path: Path, owner: bytes | None,
) -> None:
    seeded = _seed_v11(tmp_path, owner=owner)
    old_owner_hash = seeded["step_blobs"].get(f"{_PREFIX}/provider")
    source_blobs = _blob_snapshot(tmp_path)

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "legacy")
    assert migrated is not None
    mappings = migrated["step_blobs"]
    slot_path = f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}"
    manifest_path = f"{_PREFIX}/resume-state.json"
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    native = parse_native_resume_state(store.get(mappings[manifest_path]) or b"")
    selected = parse_codex_resume_state(native)
    expected_owner = old_owner_hash or hash_content(b"codex")
    assert (
        store.get(mappings[slot_path]),
        selected,
        mappings[f"{_PREFIX}/provider"],
        f"{_PREFIX}/codex_thread.json" in mappings,
        any("/codex_rollout/" in path for path in mappings),
        archive.data_version_path(tmp_path).read_bytes(),
    ) == (
        _ROLLOUT,
        CodexResumeState("thread-1", _RESTORE_PATH, CODEX_ROLLOUT_SLOTS[0]),
        expected_owner,
        False,
        False,
        b"12",
    )
    assert all(_blob_snapshot(tmp_path)[path] == raw for path, raw in source_blobs.items())

    first = _archive_snapshot(tmp_path)
    archive._migrate_archive_blobs(tmp_path)
    assert _archive_snapshot(tmp_path) == first


def test_completes_consistent_staged_slot_and_manifest(tmp_path: Path) -> None:
    rec = _seed_v11(tmp_path)
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    state = make_codex_resume_state(
        thread_id="thread-1",
        restore_path=_RESTORE_PATH,
        rollout_slot=CODEX_ROLLOUT_SLOTS[0],
    )
    rec["step_blobs"][f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}"] = store.put(_ROLLOUT)
    rec["step_blobs"][f"{_PREFIX}/resume-state.json"] = store.put(
        encode_native_resume_state(state)
    )
    _write_record(tmp_path, rec)

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "legacy")
    assert migrated is not None
    assert set(migrated["step_blobs"]) == {
        f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}",
        f"{_PREFIX}/resume-state.json",
        f"{_PREFIX}/provider",
    }


def test_rejects_current_slot_without_manifest(tmp_path: Path) -> None:
    rec = _seed_v11(tmp_path)
    rec["step_blobs"].pop(f"{_PREFIX}/codex_thread.json")
    rec["step_blobs"][f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}"] = (
        rec["step_blobs"].pop(f"{_PREFIX}/codex_rollout/{_RESTORE_PATH}")
    )
    _write_record(tmp_path, rec)
    before = _archive_snapshot(tmp_path)

    with pytest.raises(InvalidResumeState):
        archive.ensure_current(tmp_path)

    assert _archive_snapshot(tmp_path) == before


_INVALID_LEGACY = [
    b"\xff",
    b"{",
    b'{"thread_id":"","rollout_relpath":"sessions/x.jsonl"}',
    b'{"thread_id":"t","rollout_relpath":"../x.jsonl"}',
    b'{"thread_id":"t","rollout_relpath":"/sessions/x.jsonl"}',
    b'{"thread_id":"t","rollout_relpath":"other/x.jsonl"}',
    b'{"thread_id":"t","rollout_relpath":"sessions/x.txt"}',
    b'{"thread_id":"t","rollout_relpath":"sessions/x.jsonl","extra":1}',
    b'{"thread_id":"a","thread_id":"b","rollout_relpath":"sessions/x.jsonl"}',
]


@pytest.mark.parametrize("metadata", _INVALID_LEGACY)
def test_rejects_invalid_legacy_metadata_without_publication(
    tmp_path: Path, metadata: bytes,
) -> None:
    _seed_v11(tmp_path, metadata=metadata)
    before = _archive_snapshot(tmp_path)

    with pytest.raises(InvalidResumeState):
        archive.ensure_current(tmp_path)

    assert _archive_snapshot(tmp_path) == before


def test_preflights_all_records_before_first_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_v11(tmp_path, cid="a-valid")
    _seed_v11(tmp_path, cid="z-invalid", metadata=b"{")
    calls: list[str] = []
    real_put = LocalBlobStore.put
    real_write = record.write_record
    real_rebuild = archive.rebuild_index

    def put(self: LocalBlobStore, content: bytes) -> str:
        calls.append("put")
        return real_put(self, content)

    def write(records_dir: Path, rec: dict) -> Path:
        calls.append("write")
        return real_write(records_dir, rec)

    def rebuild(control: Path) -> None:
        calls.append("rebuild")
        real_rebuild(control)

    monkeypatch.setattr(LocalBlobStore, "put", put)
    monkeypatch.setattr(record, "write_record", write)
    monkeypatch.setattr(archive, "rebuild_index", rebuild)

    with pytest.raises(InvalidResumeState):
        archive.ensure_current(tmp_path)

    assert calls == []


@pytest.mark.parametrize("step_blobs", [[], None, "", 0])
def test_rejects_falsy_non_dict_step_blobs_before_publication(
    tmp_path: Path, step_blobs: object,
) -> None:
    _seed_v11(tmp_path, cid="a-valid")
    malformed = _seed_v11(tmp_path, cid="z-invalid")
    malformed["step_blobs"] = step_blobs
    _write_record(tmp_path, malformed)
    records_before = {
        path.name: path.read_bytes()
        for path in archive.records_root(tmp_path).iterdir()
    }
    blobs_before = _blob_snapshot(tmp_path)
    index_before = (
        archive.index_path(tmp_path).read_bytes()
        if archive.index_path(tmp_path).exists()
        else None
    )

    with pytest.raises(InvalidResumeState):
        archive.ensure_current(tmp_path)

    index_after = (
        archive.index_path(tmp_path).read_bytes()
        if archive.index_path(tmp_path).exists()
        else None
    )
    assert (
        {
            path.name: path.read_bytes()
            for path in archive.records_root(tmp_path).iterdir()
        },
        _blob_snapshot(tmp_path),
        index_after,
        archive.data_version_path(tmp_path).read_bytes(),
    ) == (records_before, blobs_before, index_before, b"11")


@pytest.mark.parametrize("case", ["missing-rollout", "foreign-owner"])
def test_rejects_missing_rollout_or_foreign_owner_without_mutation(
    tmp_path: Path, case: str,
) -> None:
    rec = _seed_v11(tmp_path, owner=b"claude_code" if case == "foreign-owner" else None)
    if case == "missing-rollout":
        rec["step_blobs"].pop(f"{_PREFIX}/codex_rollout/{_RESTORE_PATH}")
        _write_record(tmp_path, rec)
    before = _archive_snapshot(tmp_path)

    with pytest.raises(InvalidResumeState):
        archive.ensure_current(tmp_path)

    assert _archive_snapshot(tmp_path) == before


@pytest.mark.parametrize("case", ["wrong-slot", "malformed-manifest", "other-slot"])
def test_rejects_inconsistent_partial_current_state(
    tmp_path: Path, case: str,
) -> None:
    rec = _seed_v11(tmp_path)
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    if case == "wrong-slot":
        rec["step_blobs"][f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}"] = store.put(b"WRONG")
        state = make_codex_resume_state(
            thread_id="thread-1",
            restore_path=_RESTORE_PATH,
            rollout_slot=CODEX_ROLLOUT_SLOTS[0],
        )
        rec["step_blobs"][f"{_PREFIX}/resume-state.json"] = store.put(
            encode_native_resume_state(state)
        )
    elif case == "malformed-manifest":
        rec["step_blobs"][f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}"] = store.put(_ROLLOUT)
        rec["step_blobs"][f"{_PREFIX}/resume-state.json"] = store.put(b"{")
    else:
        rec["step_blobs"][f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[1]}"] = store.put(b"OTHER")
    _write_record(tmp_path, rec)
    before = _archive_snapshot(tmp_path)

    with pytest.raises(InvalidResumeState):
        archive.ensure_current(tmp_path)

    assert _archive_snapshot(tmp_path) == before


def test_rejects_staged_manifest_mismatching_legacy_metadata(tmp_path: Path) -> None:
    rec = _seed_v11(tmp_path)
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    rec["step_blobs"][f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}"] = store.put(_ROLLOUT)
    wrong = make_codex_resume_state(
        thread_id="other-thread",
        restore_path=_RESTORE_PATH,
        rollout_slot=CODEX_ROLLOUT_SLOTS[0],
    )
    rec["step_blobs"][f"{_PREFIX}/resume-state.json"] = store.put(
        encode_native_resume_state(wrong)
    )
    _write_record(tmp_path, rec)
    before = _archive_snapshot(tmp_path)

    with pytest.raises(InvalidResumeState):
        archive.ensure_current(tmp_path)

    assert _archive_snapshot(tmp_path) == before


def test_preserves_and_never_inspects_stashed_attempt_state(tmp_path: Path) -> None:
    rec = _seed_v11(tmp_path)
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    stash_prefix = "build/coder.attempt-7"
    stash = {
        f"{stash_prefix}/codex_thread.json": store.put(b"{"),
        f"{stash_prefix}/codex_rollout/not-safe": store.put(b"STASH"),
        f"{stash_prefix}/{CODEX_ROLLOUT_SLOTS[1]}": store.put(b"STASH-SLOT"),
    }
    rec["step_blobs"].update(stash)
    _write_record(tmp_path, rec)
    before_bytes = {path: store.get(digest) for path, digest in stash.items()}

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "legacy")
    assert migrated is not None
    assert (
        {path: migrated["step_blobs"][path] for path in stash},
        {path: store.get(migrated["step_blobs"][path]) for path in stash},
    ) == (stash, before_bytes)


def _stable_name(control: Path, path: str | Path) -> str:
    target = Path(path)
    root = archive.archive_root(control)
    name = target.name
    if name == "data_version":
        return "data_version"
    if name.startswith(".data_version-"):
        return "data_version.tmp"
    if name == "index.db":
        return "index"
    if name.endswith(".db.tmp"):
        return "index.tmp"
    records = archive.records_root(control)
    if target.parent == records:
        if name.startswith("."):
            return f"record:{name[1:].split('-', 1)[0]}.tmp"
        return f"record:{target.stem}"
    if target.parent.parent == records and name.startswith("."):
        return "record.tmp"
    blobs = archive.blobs_root(control)
    try:
        rel = target.relative_to(blobs)
    except ValueError:
        rel = None
    if rel is not None and len(rel.parts) == 2:
        leaf = rel.parts[1]
        if leaf.startswith("."):
            leaf = leaf[1:].split("-", 1)[0]
            return f"blob:{rel.parts[0]}{leaf}.tmp"
        return f"blob:{rel.parts[0]}{leaf}"
    if target == root:
        return "archive"
    if target == blobs:
        return "blobs"
    if target == records:
        return "records"
    if target.parent == blobs:
        return f"blob-shard:{target.name}"
    return target.name


class _CrashTrace:
    def __init__(self, fail_at: int | None = None) -> None:
        self.fail_at = fail_at
        self.events: list[str] = []

    def event(self, value: str) -> None:
        self.events.append(value)
        if self.fail_at == len(self.events):
            raise OSError("injected archive migration crash")


def _install_crash_trace(
    control: Path,
    monkeypatch: pytest.MonkeyPatch,
    trace: _CrashTrace,
) -> None:
    real_replace = durable.os.replace
    real_fsync = durable.os.fsync
    real_fsync_directory = durable.fsync_directory
    real_durable_replace = durable.durable_replace
    real_commit = index._commit

    def traced_durable_replace(path: Path, writer: Callable) -> None:
        def traced_writer(stream):
            trace.event(f"write:{_stable_name(control, path)}")
            return writer(stream)
        real_durable_replace(path, traced_writer)

    def traced_fsync(fd: int) -> None:
        path = Path(f"/proc/self/fd/{fd}").resolve()
        if not path.is_dir():
            trace.event(f"file-fsync:{_stable_name(control, path)}")
        real_fsync(fd)

    def traced_replace(source, target) -> None:
        trace.event(f"replace:{_stable_name(control, target)}")
        real_replace(source, target)

    def traced_fsync_directory(path: Path) -> None:
        trace.event(f"directory-fsync:{_stable_name(control, path)}")
        real_fsync_directory(path)

    def traced_commit(conn: sqlite3.Connection) -> None:
        trace.event("index-commit:index.tmp")
        real_commit(conn)

    monkeypatch.setattr(durable, "durable_replace", traced_durable_replace)
    monkeypatch.setattr(durable.os, "fsync", traced_fsync)
    monkeypatch.setattr(durable.os, "replace", traced_replace)
    monkeypatch.setattr(durable, "fsync_directory", traced_fsync_directory)
    monkeypatch.setattr(index, "_commit", traced_commit)


def _subsequence_before(events: list[str], expected: list[str], before: int) -> None:
    cursor = 0
    for event in events[:before]:
        if event == expected[cursor]:
            cursor += 1
            if cursor == len(expected):
                return
    raise AssertionError(f"missing ordered barrier sequence: {expected!r}")


def _assert_dependency_barriers_precede_record(
    control: Path,
    initial_blobs: dict[str, bytes],
    events: list[str],
    cid: str,
) -> None:
    rec = record.read_record(archive.records_root(control), cid)
    assert rec is not None
    record_write = events.index(f"write:record:{cid}")
    for path, digest in rec["step_blobs"].items():
        if path.rsplit("/", 1)[-1] not in {
            *CODEX_ROLLOUT_SLOTS, "resume-state.json", "provider",
        } or ".attempt-" in path.split("/")[1]:
            continue
        blob_name = f"blob:{digest}"
        existed = f"{digest[:2]}/{digest[2:]}" in initial_blobs
        if existed:
            expected = [
                f"file-fsync:{blob_name}",
                f"directory-fsync:blob-shard:{digest[:2]}",
                "directory-fsync:blobs",
                "directory-fsync:archive",
            ]
        else:
            expected = [
                f"write:{blob_name}",
                f"file-fsync:{blob_name}.tmp",
                f"replace:{blob_name}",
                f"directory-fsync:blob-shard:{digest[:2]}",
                "directory-fsync:blobs",
                "directory-fsync:archive",
            ]
        _subsequence_before(events, expected, record_write)


def test_current_v11_two_slot_state_is_republished_after_dependency_barriers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = _seed_current_v11(tmp_path)
    record_path = archive.records_root(tmp_path) / "current.json"
    record_bytes = record_path.read_bytes()
    initial_blobs = _blob_snapshot(tmp_path)
    trace = _CrashTrace()
    _install_crash_trace(tmp_path, monkeypatch, trace)

    archive.ensure_current(tmp_path)

    migrated = record.read_record(archive.records_root(tmp_path), "current")
    assert migrated is not None
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    native = parse_native_resume_state(
        store.get(migrated["step_blobs"][f"{_PREFIX}/resume-state.json"]) or b"")
    _assert_dependency_barriers_precede_record(
        tmp_path, initial_blobs, trace.events, "current")
    assert (
        migrated["step_blobs"],
        record_path.read_bytes(),
        store.get(migrated["step_blobs"][f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[0]}"]),
        store.get(migrated["step_blobs"][f"{_PREFIX}/{CODEX_ROLLOUT_SLOTS[1]}"]),
        parse_codex_resume_state(native).rollout_slot,
    ) == (
        seeded["step_blobs"],
        record_bytes,
        b"SLOT-A\n",
        b"SLOT-B\n",
        CODEX_ROLLOUT_SLOTS[1],
    )
    record_events = [
        "write:record:current",
        "file-fsync:record:current.tmp",
        "replace:record:current",
        "directory-fsync:records",
    ]
    _subsequence_before(trace.events, record_events, len(trace.events))


def test_missing_published_dependency_does_not_publish_record_or_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_v11(tmp_path)
    record_path = archive.records_root(tmp_path) / "legacy.json"
    record_bytes = record_path.read_bytes()

    def missing(self: LocalBlobStore, digest: str) -> bool:
        return False

    monkeypatch.setattr(LocalBlobStore, "has", missing)

    with pytest.raises(OSError, match=hash_content(_ROLLOUT)):
        archive.ensure_current(tmp_path)

    assert (
        record_path.read_bytes(),
        archive.index_path(tmp_path).exists(),
        archive.data_version_path(tmp_path).read_bytes(),
    ) == (record_bytes, False, b"11")


@pytest.mark.parametrize("seed", [_seed_v11, _seed_current_v11], ids=["legacy", "current"])
def test_archive_codex_migration_is_rerunnable_at_every_crash_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    seed: Callable[[Path], dict],
) -> None:
    base = tmp_path / "base"
    base.mkdir()
    seed(base)
    initial_blobs = _blob_snapshot(base)
    expected_root = tmp_path / "expected"
    expected_root.mkdir()
    shutil.copytree(
        archive.archive_root(base), archive.archive_root(expected_root))
    probe = _CrashTrace()
    with monkeypatch.context() as patch:
        _install_crash_trace(expected_root, patch, probe)
        archive.ensure_current(expected_root)
    expected_archive = _archive_snapshot(expected_root)
    expected_index = _index_snapshot(expected_root)
    cid = "legacy" if seed is _seed_v11 else "current"

    assert probe.events[-4:] == [
        "write:data_version",
        "file-fsync:data_version.tmp",
        "replace:data_version",
        "directory-fsync:archive",
    ]
    assert {event.split(":", 1)[0] for event in probe.events} == {
        "write", "file-fsync", "replace", "directory-fsync", "index-commit",
    }
    _assert_dependency_barriers_precede_record(
        expected_root, initial_blobs, probe.events, cid)
    record_directory = probe.events.index("directory-fsync:records")
    commits = [
        i for i, event in enumerate(probe.events) if event == "index-commit:index.tmp"
    ]
    index_fsync = probe.events.index("file-fsync:index.tmp")
    assert len(commits) == 2
    assert record_directory < commits[0] < commits[1] < index_fsync
    assert "replace:index" in probe.events
    assert "directory-fsync:archive" in probe.events[index_fsync:]

    for fail_at in range(1, len(probe.events) + 1):
        crashed = tmp_path / f"crash-{fail_at}"
        crashed.mkdir()
        shutil.copytree(
            archive.archive_root(base), archive.archive_root(crashed))
        trace = _CrashTrace(fail_at)
        with monkeypatch.context() as patch:
            _install_crash_trace(crashed, patch, trace)
            with pytest.raises(OSError, match="injected archive migration crash"):
                archive.ensure_current(crashed)
        marker = archive.data_version_path(crashed).read_text().strip()
        assert marker in {"11", "12"}
        if marker == "11":
            archive.ensure_current(crashed)
        assert archive.data_version_path(crashed).read_bytes() == b"12"
        assert _archive_snapshot(crashed) == expected_archive
        assert _index_snapshot(crashed) == expected_index
        migrated = record.read_record(archive.records_root(crashed), cid)
        assert migrated is not None
        store = LocalBlobStore(archive.blobs_root(crashed))
        assert all(store.has(digest) for digest in migrated["step_blobs"].values())


@pytest.mark.parametrize("step_blobs", [[("path", "digest")], {"path": 1}])
def test_rejects_invalid_step_blobs_shape(
    tmp_path: Path, step_blobs: object,
) -> None:
    rec = _seed_v11(tmp_path)
    rec["step_blobs"] = step_blobs
    _write_record(tmp_path, rec)
    before = _archive_snapshot(tmp_path)

    with pytest.raises(InvalidResumeState):
        archive.ensure_current(tmp_path)

    assert _archive_snapshot(tmp_path) == before


@pytest.mark.parametrize("owner", [None, b"claude_code"])
def test_rejects_current_codex_state_without_codex_owner(
    tmp_path: Path, owner: bytes | None,
) -> None:
    rec = _seed_current_v11(tmp_path)
    store = LocalBlobStore(archive.blobs_root(tmp_path))
    if owner is None:
        rec["step_blobs"].pop(f"{_PREFIX}/provider")
    else:
        rec["step_blobs"][f"{_PREFIX}/provider"] = store.put(owner)
    _write_record(tmp_path, rec)
    before = _archive_snapshot(tmp_path)

    with pytest.raises(InvalidResumeState):
        archive.ensure_current(tmp_path)

    assert _archive_snapshot(tmp_path) == before
