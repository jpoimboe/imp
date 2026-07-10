"""Transient read-fault recovery and atomic writes for archive record files.

Same phenomenon as test_state_transient_recovery: btrfs+zstd can transiently
serve a mis-decompressed (head-dropped) cached copy of a file that is valid on
disk. guno14's `imp resume` crashed at the post-merge exit-code read when the
archive-record fallback (`read_record`) hit that fault with no retry. Records
get the same cold re-read recovery as State.load, and write_record writes
atomically so a concurrent reader can never see a torn (empty/prefix) file.
"""
from __future__ import annotations

import json
import multiprocessing
import time
from pathlib import Path

import pytest

from imp.engine.archive import record


def _quest_meta() -> dict:
    return {
        "id": "alpha",
        "title": "Alpha",
        "base_branch": "main",
        "created_at": "2026-06-01T00:00:00+00:00",
    }


def _rec() -> dict:
    return record.build_record(
        _quest_meta(), {"id": "alpha", "status": "completed"}, "hash123")


def test_read_record_recovers_from_transient_head_drop(
        tmp_path: Path, monkeypatch) -> None:
    # On-disk file is VALID; only the first read returns the head-dropped bytes.
    records_dir = tmp_path / "records"
    record.write_record(records_dir, _rec())
    good = (records_dir / "alpha.json").read_bytes()
    real = record._read_record_bytes
    calls = {"n": 0}

    def flaky(path: Path) -> bytes:
        calls["n"] += 1
        return good[1:] if calls["n"] == 1 else real(path)

    monkeypatch.setattr(record, "_read_record_bytes", flaky)
    assert record.read_record(records_dir, "alpha") == _rec()
    assert calls["n"] >= 2               # at least one retry happened


def test_read_record_persistent_corruption_raises_after_retries(
        tmp_path: Path) -> None:
    # Genuinely corrupt on-disk bytes: every re-read returns the same bad bytes,
    # so retries cannot recover and the decode error surfaces (no fail-soft).
    # The corrupt file itself stays on disk as the forensic evidence.
    records_dir = tmp_path / "records"
    records_dir.mkdir(parents=True)
    bad = b'{"id": "bad"}\n{"trailing": true}\n'
    (records_dir / "bad.json").write_bytes(bad)
    with pytest.raises(json.JSONDecodeError):
        record.read_record(records_dir, "bad")
    assert (records_dir / "bad.json").read_bytes() == bad


def _hammer_writer(records_dir: str, stop: float) -> None:
    from imp.engine.archive import record as rec_mod
    rec = rec_mod.build_record(
        {"id": "alpha", "title": "Alpha", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00"},
        {"id": "alpha", "status": "completed", "vars": {"pad": "x" * 8192}},
        "hash123")
    while time.monotonic() < stop:
        rec_mod.write_record(Path(records_dir), rec)


def test_write_record_never_exposes_torn_file(tmp_path: Path) -> None:
    # A reader racing write_record must only ever observe a complete record:
    # either the file is absent, or read bytes parse. A truncate-then-write
    # writer fails this within milliseconds (empty/prefix reads).
    records_dir = tmp_path / "records"
    records_dir.mkdir(parents=True)
    path = records_dir / "alpha.json"
    stop = time.monotonic() + 0.6
    ctx = multiprocessing.get_context("spawn")
    writer = ctx.Process(target=_hammer_writer, args=(str(records_dir), stop))
    writer.start()
    try:
        reads = 0
        while time.monotonic() < stop:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                continue
            reads += 1
            json.loads(raw)              # any torn state fails here
    finally:
        writer.join(timeout=10)
    assert reads > 0                     # the race actually ran


def test_write_record_leaves_no_temp_droppings(tmp_path: Path) -> None:
    records_dir = tmp_path / "records"
    record.write_record(records_dir, _rec())
    assert sorted(p.name for p in records_dir.iterdir()) == ["alpha.json"]
