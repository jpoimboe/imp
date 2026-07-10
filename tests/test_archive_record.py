from __future__ import annotations

from pathlib import Path

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
    return {"id": "alpha", "status": "completed", "step": "finalize",
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
