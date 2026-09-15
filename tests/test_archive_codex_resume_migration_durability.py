from __future__ import annotations

import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from imp.engine import archive, durable
from imp.engine.archive import index, record
from imp.engine.archive.blobstore import LocalBlobStore, hash_content
from imp.engine.resume_state import parse_native_resume_state
from imp.providers.codex_rollout import CODEX_ROLLOUT_SLOTS, parse_codex_resume_state
from tests._archive_codex_resume_migration import (
    _PREFIX,
    _ROLLOUT,
    _archive_snapshot,
    _blob_snapshot,
    _index_snapshot,
    _seed_current_v11,
    _seed_v11,
)


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
        assert marker in {"11", "13"}
        if marker == "11":
            archive.ensure_current(crashed)
        assert archive.data_version_path(crashed).read_bytes() == b"13"
        assert _archive_snapshot(crashed) == expected_archive
        assert _index_snapshot(crashed) == expected_index
        migrated = record.read_record(archive.records_root(crashed), cid)
        assert migrated is not None
        store = LocalBlobStore(archive.blobs_root(crashed))
        assert all(store.has(digest) for digest in migrated["step_blobs"].values())
