from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import impty.journal
from impty.journal import Journal


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_the_index_names_both_files(tmp_path: Path) -> None:
    Journal(tmp_path, 2).end(0)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["pty-2.jsonl", "pty-2.raw"]


def test_start_record_carries_the_argv_and_geometry(tmp_path: Path) -> None:
    j = Journal(tmp_path, 1)
    j.start(["imp", "list"], cols=80, rows=24)
    j.end(0)
    first = _records(tmp_path / "pty-1.jsonl")[0]
    assert first["ev"] == "start"
    assert first["argv"] == ["imp", "list"]
    assert (first["cols"], first["rows"], first["off"]) == (80, 24, 0)


def test_output_is_byte_exact_and_advances_the_record_offsets(tmp_path: Path) -> None:
    j = Journal(tmp_path, 1)
    j.start(["prog"], cols=80, rows=24)
    j.output(b"\x1b[>4;2mabc")  # 10 bytes, private-CSI prefix kept verbatim
    j.output(b"de")
    j.send(3)
    j.output(b"f")
    j.end(0)
    assert (tmp_path / "pty-1.raw").read_bytes() == b"\x1b[>4;2mabcdef"
    assert [(r["ev"], r["off"]) for r in _records(tmp_path / "pty-1.jsonl")] == [
        ("start", 0), ("send", 12), ("end", 13),
    ]


def test_every_line_is_json_carrying_t_off_and_ev(tmp_path: Path) -> None:
    j = Journal(tmp_path, 1)
    j.start(["prog"], cols=80, rows=24)
    j.output(b"x")
    j.resize(40, 120)
    j.send(5)
    j.end(-15)
    recs = _records(tmp_path / "pty-1.jsonl")
    assert [r["ev"] for r in recs] == ["start", "resize", "send", "end"]
    for r in recs:
        assert isinstance(r["t"], int | float)
        assert isinstance(r["off"], int)
        assert isinstance(r["ev"], str)
    assert (recs[1]["cols"], recs[1]["rows"]) == (120, 40)
    assert recs[2]["len"] == 5
    assert recs[3]["exit"] == -15


def test_writes_reach_disk_before_the_journal_is_closed(tmp_path: Path) -> None:
    j = Journal(tmp_path, 1)
    j.start(["prog"], cols=80, rows=24)
    j.output(b"live")
    # Fresh handles, no close yet: a buffered writer shows nothing here.
    assert (tmp_path / "pty-1.raw").read_bytes() == b"live"
    assert _records(tmp_path / "pty-1.jsonl")[0]["ev"] == "start"
    j.end(0)


def test_an_output_in_flight_when_end_arrives_is_serialized_not_torn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # close() writes `end` after releasing the session's _io_lock, so it really
    # can land while another thread is inside output(). The journal's own lock is
    # what keeps that from writing to (or through) a half-closed handle.
    j = Journal(tmp_path, 1)
    j.start(["prog"], cols=80, rows=24)
    inside = threading.Event()
    real = impty.journal._write_all

    def slow(fh: Any, data: bytes) -> None:
        if data == b"SLOW":
            inside.set()
            time.sleep(0.2)  # widen the window; not a timing assertion
        real(fh, data)

    monkeypatch.setattr(impty.journal, "_write_all", slow)
    errors: list[BaseException] = []

    def writer() -> None:
        try:
            j.output(b"SLOW")
        except Exception as exc:
            errors.append(exc)

    t = threading.Thread(target=writer)
    t.start()
    assert inside.wait(timeout=30.0)
    j.end(0)
    t.join(timeout=30.0)
    assert not t.is_alive()
    assert errors == []
    assert (tmp_path / "pty-1.raw").read_bytes() == b"SLOW"
    # end waited for the chunk, so it names the offset past it.
    last = _records(tmp_path / "pty-1.jsonl")[-1]
    assert (last["ev"], last["off"]) == ("end", 4)
