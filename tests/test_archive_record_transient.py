"""Transient read-fault recovery and atomic writes for archive record files.

Same phenomenon as test_state_transient_recovery: btrfs+zstd can transiently
serve a mis-decompressed (head-dropped) cached copy of a file that is valid on
disk. guno14's `imp resume` crashed at the post-merge exit-code read when the
archive-record fallback (`read_record`) hit that fault with no retry. Records
get the same cold re-read recovery as State.load, and write_record writes
atomically so a concurrent reader can never see a torn (empty/prefix) file.
"""
from __future__ import annotations

import hashlib
import json
import multiprocessing
import os
from pathlib import Path

import pytest

from imp.engine import readfault
from imp.engine.archive import record
from imp.engine.state import InMemoryEventLog
from tests._faultlog import diag, fault_lines


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


_MIN_READS = 50
_MIN_WRITES = 50


def _hammer_writer(records_dir: str, stop, writes) -> None:
    from imp.engine.archive import record as rec_mod
    rec = rec_mod.build_record(
        {"id": "alpha", "title": "Alpha", "base_branch": "main",
         "created_at": "2026-06-01T00:00:00+00:00"},
        {"id": "alpha", "status": "completed", "vars": {"pad": "x" * 8192}},
        "hash123")
    while not stop.is_set():
        rec_mod.write_record(Path(records_dir), rec)
        writes.value += 1


def test_write_record_never_exposes_torn_file(tmp_path: Path) -> None:
    # No clock here: the loops exit on proven overlap; pyproject's 60s timeout catches hangs.
    records_dir = tmp_path / "records"
    records_dir.mkdir(parents=True)
    path = records_dir / "alpha.json"
    ctx = multiprocessing.get_context("spawn")
    stop = ctx.Event()
    writes = ctx.Value("i", 0)
    writer = ctx.Process(
        target=_hammer_writer, args=(str(records_dir), stop, writes))
    writer.start()
    pid = writer.pid
    reads = 0
    try:
        while not path.exists():
            os.sched_yield()
        base = writes.value
        while reads < _MIN_READS or writes.value - base < _MIN_WRITES:
            try:
                raw = path.read_bytes()
            except FileNotFoundError:
                continue
            reads += 1
            json.loads(raw)              # any torn state fails here
    finally:
        stop.set()
        writer.join(timeout=30)
        if writer.is_alive():
            writer.kill()
            writer.join(timeout=30)
    assert reads >= _MIN_READS
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)                  # joined => reaped, not a zombie


def test_write_record_leaves_no_temp_droppings(tmp_path: Path) -> None:
    records_dir = tmp_path / "records"
    record.write_record(records_dir, _rec())
    assert sorted(p.name for p in records_dir.iterdir()) == ["alpha.json"]


def test_recovered_record_fault_appends_one_fingerprinted_line(
        tmp_path: Path, monkeypatch) -> None:
    records_dir = tmp_path / "records"
    record.write_record(records_dir, _rec())
    path = records_dir / "alpha.json"
    good = path.read_bytes()
    real = record._read_record_bytes
    calls = {"n": 0}

    def flaky(p: Path) -> bytes:
        calls["n"] += 1
        return good[1:] if calls["n"] == 1 else real(p)

    monkeypatch.setattr(record, "_read_record_bytes", flaky)
    assert record.read_record(records_dir, "alpha") == _rec()
    lines = fault_lines()
    assert len(lines) == 1
    line = lines[0]
    assert line["site"] == "archive_record"
    assert line["path"] == str(path)
    assert line["recovered"] is True
    assert line["attempt"] >= 1
    assert line["head_drop"] is True
    assert line["tail_drop"] is False
    assert line["bad_len"] == len(good) - 1
    assert line["good_len"] == len(good)
    assert line["bad_sha1"] == hashlib.sha1(good[1:]).hexdigest()
    assert line["good_sha1"] == hashlib.sha1(good).hexdigest()
    assert (diag() / line["bytes"]).read_bytes() == good[1:]


def test_unrecovered_record_fault_records_one_line_with_the_good_side_null(
        tmp_path: Path) -> None:
    records_dir = tmp_path / "records"
    records_dir.mkdir(parents=True)
    bad = b'{"id": "bad"}\n{"trailing": true}\n'
    (records_dir / "bad.json").write_bytes(bad)
    with pytest.raises(json.JSONDecodeError):
        record.read_record(records_dir, "bad")
    lines = fault_lines()
    assert len(lines) == 1
    line = lines[0]
    assert line["site"] == "archive_record"
    assert line["recovered"] is False
    assert line["attempt"] is None
    assert line["good_len"] is None
    assert line["head_drop"] is None
    assert line["tail_drop"] is None
    assert line["good_sha1"] is None
    assert line["bad_len"] == len(bad)
    assert line["bad_sha1"] == hashlib.sha1(bad).hexdigest()
    assert (diag() / line["bytes"]).read_bytes() == bad


def test_record_recovery_under_a_sink_names_the_record_file(
        tmp_path: Path, monkeypatch) -> None:
    records_dir = tmp_path / "records"
    record.write_record(records_dir, _rec())
    good = (records_dir / "alpha.json").read_bytes()
    real = record._read_record_bytes
    calls = {"n": 0}

    def flaky(p: Path) -> bytes:
        calls["n"] += 1
        return good[:-1] if calls["n"] == 1 else real(p)

    monkeypatch.setattr(record, "_read_record_bytes", flaky)
    log = InMemoryEventLog()
    with readfault.event_sink(log):
        assert record.read_record(records_dir, "alpha") == _rec()
    warnings = [e for e in log.read() if e["type"] == "runner_warning"]
    assert len(warnings) == 1
    message = warnings[0]["payload"]["message"]
    assert message.startswith(
        "transient read fault recovered — archive record alpha.json "
        "re-read cleanly on attempt ")
    assert message.endswith("(tail-drop)")
    assert str(records_dir / "alpha.json") in warnings[0]["payload"]["detail"]
