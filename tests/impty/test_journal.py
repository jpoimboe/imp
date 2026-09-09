from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

import impty.journal
from impty import TerminalSession
from impty.journal import Journal
from tests.impty.e2e_support import _poll, can_pty

_needs_pty = pytest.mark.skipif(not can_pty(), reason="pty not available")

# Generous enough never to false-fire under a saturated -n auto run, and well
# inside the 60 s per-test outer timeout that catches a genuine hang.
_JOURNAL_POLL = 30.0

_NEUTRAL_CHILD = r"""
import os
os.write(1, b"ALPHA-BETA-GAMMA")
"""

_NEWLINE_CHILD = r"""
import os
os.write(1, b"hello\nworld\n")
"""

_FILTERED_CHILD = r"""
import os, time
os.write(1, b"\x1b[>4;2mVISIBLE\r\n")
time.sleep(30)
"""

# Emits SECOND only once the test creates the flag file — so SECOND cannot exist
# while any session call is reading, whatever the scheduler does under load.
_FLAGGED_CHILD = r"""
import os, sys, time
os.write(1, b"FIRST\r\n")
while not os.path.exists(sys.argv[1]):
    time.sleep(0.02)
os.write(1, b"SECOND\r\n")
time.sleep(30)
"""

_MARKER_CHILD = r"""
import os, time
os.write(1, b"MARKER\r\n")
time.sleep(30)
"""


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


@_needs_pty
def test_start_record_matches_the_spawn(tmp_path: Path) -> None:
    argv = [sys.executable, "-c", _NEUTRAL_CHILD]
    with TerminalSession(argv, cols=100, rows=30, journal=Journal(tmp_path, 1)) as s:
        s.run_to_completion(hard=30.0)
    first = _records(tmp_path / "pty-1.jsonl")[0]
    assert first["ev"] == "start"
    assert first["argv"] == argv
    assert (first["cols"], first["rows"]) == (100, 30)


@_needs_pty
def test_raw_is_byte_exact_for_a_line_discipline_neutral_payload(tmp_path: Path) -> None:
    with TerminalSession(
        [sys.executable, "-c", _NEUTRAL_CHILD], journal=Journal(tmp_path, 1),
    ) as s:
        s.run_to_completion(hard=30.0)
    assert (tmp_path / "pty-1.raw").read_bytes() == b"ALPHA-BETA-GAMMA"


@_needs_pty
def test_onlcr_expansion_is_recorded_as_delivered(tmp_path: Path) -> None:
    # The master delivers what the line discipline produced, not the child's
    # write() buffer: every \n arrives as \r\n and is journaled that way.
    with TerminalSession(
        [sys.executable, "-c", _NEWLINE_CHILD], journal=Journal(tmp_path, 1),
    ) as s:
        s.run_to_completion(hard=30.0)
    assert (tmp_path / "pty-1.raw").read_bytes() == b"hello\r\nworld\r\n"


@_needs_pty
def test_raw_holds_the_bytes_the_stream_filter_drops(tmp_path: Path) -> None:
    with TerminalSession(
        [sys.executable, "-c", _FILTERED_CHILD], journal=Journal(tmp_path, 1),
    ) as s:
        assert s.wait_idle(idle=0.2, hard=30.0)
        text = "\n".join(s.lines())
    assert b"\x1b[>4;2m" in (tmp_path / "pty-1.raw").read_bytes()
    assert "VISIBLE" in text
    assert "4;2m" not in text


@_needs_pty
def test_bytes_the_background_drainer_reads_are_journaled(tmp_path: Path) -> None:
    raw = tmp_path / "pty-1.raw"
    flag = tmp_path / "go"
    with TerminalSession(
        [sys.executable, "-c", _FLAGGED_CHILD, str(flag)], journal=Journal(tmp_path, 1),
    ) as s:
        assert s.wait_idle(idle=0.2, hard=30.0)
        assert b"FIRST" in raw.read_bytes()
        # Released only now, after the last session call returned: nothing but the
        # background drainer can read SECOND, so capture must sit at the shared
        # drain point rather than in a caller.
        flag.touch()
        assert _poll(lambda: b"SECOND" in raw.read_bytes(), timeout=_JOURNAL_POLL)


@_needs_pty
def test_output_and_start_record_are_on_disk_before_close(tmp_path: Path) -> None:
    raw = tmp_path / "pty-1.raw"
    with TerminalSession(
        [sys.executable, "-c", _MARKER_CHILD], journal=Journal(tmp_path, 1),
    ) as s:
        # Each poll opens a fresh handle sharing no buffer with the writer.
        assert _poll(lambda: b"MARKER" in raw.read_bytes(), timeout=_JOURNAL_POLL)
        assert [r["ev"] for r in _records(tmp_path / "pty-1.jsonl")] == ["start"]
        assert s.is_alive()


@_needs_pty
def test_a_session_without_a_journal_writes_nothing(tmp_path: Path) -> None:
    with TerminalSession([sys.executable, "-c", _NEUTRAL_CHILD], journal=None) as s:
        s.run_to_completion(hard=30.0)
        assert "ALPHA-BETA-GAMMA" in "\n".join(s.lines())
    assert list(tmp_path.iterdir()) == []
