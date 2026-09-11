from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.engine import archive
from imp.engine.archive import record
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
from tests._archive_codex_resume_migration import (
    _PREFIX,
    _RESTORE_PATH,
    _ROLLOUT,
    _archive_snapshot,
    _blob_snapshot,
    _plan_blob,
    _seed_current_v11,
    _seed_v11,
    _write_record,
)


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
