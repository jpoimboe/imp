from __future__ import annotations

from pathlib import Path

from imp.engine import durable
from imp.engine.archive import record


def _quest_meta() -> dict:
    return {
        "id": "alpha",
        "title": "Alpha",
        "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00",
        "depends_on": ["beta"],
    }


def _final_state() -> dict:
    return {"id": "alpha", "status": "merged", "step": "finalize",
            "vars": {"k": 1}, "merge_commit": "abc123"}


def test_build_record_maps_fields_from_change_and_state() -> None:
    rec = record.build_record(_quest_meta(), _final_state(), "hash123")
    assert rec == {
        "id": "alpha",
        "title": "Alpha",
        "base": "main",
        "created_at": "2026-06-01T00:00:00+00:00",
        "depends_on": ["beta"],
        "quest_meta": _quest_meta(),
        "resumable": False,
        "final_state": _final_state(),
        "events_hash": "hash123",
        "step_blobs": {},
    }


def test_build_record_absent_depends_on() -> None:
    meta = {k: v for k, v in _quest_meta().items() if k != "depends_on"}
    rec = record.build_record(meta, _final_state(), "hash123")
    assert rec["depends_on"] == []


def test_write_then_read_record_roundtrips(tmp_path: Path) -> None:
    records_dir = tmp_path / "records"
    rec = record.build_record(_quest_meta(), _final_state(), "hash123")
    path = record.write_record(records_dir, rec)
    assert path == records_dir / "alpha.json"
    assert record.read_record(records_dir, "alpha") == rec


def test_write_record_replaces_before_fsyncing_records_directory(
    tmp_path: Path, monkeypatch,
) -> None:
    records_dir = tmp_path / "records"
    rec = record.build_record(_quest_meta(), _final_state(), "hash123")
    calls: list[tuple[str, Path]] = []
    real_fsync_file = durable._fsync_file
    real_replace = durable.os.replace
    real_fsync_directory = durable.fsync_directory

    def fsync_file(stream, path: Path) -> None:
        calls.append(("file", path.parent))
        real_fsync_file(stream, path)

    def replace(src, target) -> None:
        calls.append(("replace", Path(target)))
        real_replace(src, target)

    def fsync_directory(path: Path) -> None:
        calls.append(("directory", path))
        real_fsync_directory(path)

    monkeypatch.setattr(durable, "_fsync_file", fsync_file)
    monkeypatch.setattr(durable.os, "replace", replace)
    monkeypatch.setattr(durable, "fsync_directory", fsync_directory)

    path = record.write_record(records_dir, rec)

    assert (calls, record.read_record(records_dir, "alpha")) == (
        [
            ("file", records_dir),
            ("replace", path),
            ("directory", records_dir),
        ],
        rec,
    )


def test_read_record_absent_returns_none(tmp_path: Path) -> None:
    assert record.read_record(tmp_path / "records", "nope") is None


def test_list_record_ids(tmp_path: Path) -> None:
    records_dir = tmp_path / "records"
    meta_z = {**_quest_meta(), "id": "zeta"}
    record.write_record(records_dir, record.build_record(meta_z, _final_state(), "h"))
    record.write_record(records_dir, record.build_record(_quest_meta(), _final_state(), "h"))
    assert record.list_record_ids(records_dir) == ["alpha", "zeta"]


def test_list_record_ids_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert record.list_record_ids(tmp_path / "nope") == []


def test_build_record_stores_full_meta_and_resumable_flag() -> None:
    meta = {**_quest_meta(), "materialized": False, "orig_base_head": "abc999"}
    rec = record.build_record(meta, _final_state(), "h", resumable=True)
    assert rec["resumable"] is True
    assert rec["quest_meta"] == meta            # whole quest.json preserved
    assert rec["quest_meta"]["orig_base_head"] == "abc999"


def test_build_record_carries_step_blobs_manifest() -> None:
    rec = record.build_record(
        _quest_meta(), _final_state(), "hash123",
        resumable=True,
        step_blobs={"p/s/transcript.jsonl": "deadbeef"})
    assert rec["step_blobs"] == {"p/s/transcript.jsonl": "deadbeef"}


def test_build_record_step_blobs_default_empty() -> None:
    rec = record.build_record(_quest_meta(), _final_state(), "hash123")
    assert rec["step_blobs"] == {}
