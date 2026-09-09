from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from collections.abc import Callable
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

_SLEEPER = "import time; time.sleep(30)"

_HUNDRED_CHILD = r"""
import os, time
os.write(1, b"X" * 100)
time.sleep(30)
"""

_COOKED_CHILD = r"""
import sys, time
sys.stdout.write("READY\n")
sys.stdout.flush()
line = sys.stdin.readline()
sys.stdout.write("GOT=%d\n" % len(line))
sys.stdout.flush()
time.sleep(30)
"""

# Raw mode: no ECHO, no OPOST. The child proves the CPR reply reached it by
# reporting the reply's LENGTH rather than echoing it, so a reply found in
# .raw could only have been put there by the journal itself.
_RAW_MODE_CHILD = r"""
import os, select, time, tty
tty.setraw(0)
os.write(1, b"READY\r\n")
os.write(1, b"\x1b[6n")
reply = os.read(0, 32) if select.select([0], [], [], 20.0)[0] else b""
os.write(1, b"CPR=%d\r\n" % len(reply))
typed = os.read(0, 64) if select.select([0], [], [], 20.0)[0] else b""
os.write(1, b"TYPED=%d\r\n" % len(typed))
time.sleep(30)
"""


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _fail_writes_to(suffix: str) -> Callable[[Any, bytes], None]:
    """A _write_all that raises on one of the two files, as a full disk would."""
    real = impty.journal._write_all

    def failing(fh: Any, data: bytes) -> None:
        if fh.name.endswith(suffix):
            raise OSError("simulated ENOSPC")
        real(fh, data)

    return failing


def test_an_unopenable_dir_disables_the_journal_and_logs_once(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    # A non-existent parent, not mode bits: uid 0 writes into a 0o500 dir, and
    # imp's own gates run as root inside the sandbox.
    missing = tmp_path / "nope" / "deeper"
    with caplog.at_level(logging.WARNING, logger="impty.journal"):
        j = Journal(missing, 1)
        j.start(["prog"], cols=80, rows=24)
        j.output(b"data")
        j.send(4)
        j.end(0)
    assert not missing.exists()
    assert list(tmp_path.iterdir()) == []
    assert len([r for r in caplog.records if r.name == "impty.journal"]) == 1


@_needs_pty
def test_an_unopenable_journal_dir_leaves_the_session_working(tmp_path: Path) -> None:
    missing = tmp_path / "nope" / "deeper"
    with TerminalSession(
        [sys.executable, "-c", _COOKED_CHILD], journal=Journal(missing, 1),
    ) as s:
        assert s.wait_idle(idle=0.2, hard=30.0)
        s.send("hello\r")
        s.resize(40, 120)
        assert _poll(
            lambda: "GOT=6" in "\n".join(s.lines()), timeout=_JOURNAL_POLL,
        )
    assert list(tmp_path.iterdir()) == []


def test_a_sidecar_write_failure_is_contained_and_logged_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    j = Journal(tmp_path, 1)
    j.start(["prog"], cols=80, rows=24)
    monkeypatch.setattr(impty.journal, "_write_all", _fail_writes_to(".jsonl"))
    with caplog.at_level(logging.WARNING, logger="impty.journal"):
        j.send(4)  # the failing write
        j.output(b"more")
        j.resize(40, 120)
        j.end(0)
    assert [r["ev"] for r in _records(tmp_path / "pty-1.jsonl")] == ["start"]
    assert (tmp_path / "pty-1.raw").read_bytes() == b""  # disabled, not just sidecar
    warnings = [r for r in caplog.records if r.name == "impty.journal"]
    assert len(warnings) == 1
    assert "pty-1.jsonl" in warnings[0].getMessage()


@_needs_pty
def test_a_raw_write_failure_mid_drive_leaves_the_session_working(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture,
) -> None:
    raw = tmp_path / "pty-1.raw"
    with (
        caplog.at_level(logging.WARNING, logger="impty.journal"),
        TerminalSession(
            [sys.executable, "-c", _COOKED_CHILD], journal=Journal(tmp_path, 1),
        ) as s,
    ):
        assert s.wait_idle(idle=0.2, hard=30.0)
        assert b"READY" in raw.read_bytes()  # journaling until the fault is injected
        monkeypatch.setattr(impty.journal, "_write_all", _fail_writes_to(".raw"))
        s.send("hello\r")
        # The drive is untouched: the child still receives its keystrokes and pyte
        # still receives the output, though the journal has gone dark.
        assert _poll(lambda: "GOT=6" in "\n".join(s.lines()), timeout=_JOURNAL_POLL)
    warnings = [r for r in caplog.records if r.name == "impty.journal"]
    assert len(warnings) == 1
    assert "pty-1.raw" in warnings[0].getMessage()


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
def test_cooked_echo_lands_in_raw_while_the_sidecar_records_only_the_length(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "pty-1.raw"
    sidecar = tmp_path / "pty-1.jsonl"
    with TerminalSession(
        [sys.executable, "-c", _COOKED_CHILD], journal=Journal(tmp_path, 1),
    ) as s:
        assert s.wait_idle(idle=0.2, hard=30.0)
        s.send("SECRETKEYSTROKE\r")
        assert _poll(lambda: b"GOT=16" in raw.read_bytes(), timeout=_JOURNAL_POLL)
    sends = [r for r in _records(sidecar) if r["ev"] == "send"]
    assert [r["len"] for r in sends] == [16]
    assert "SECRETKEYSTROKE" not in sidecar.read_text()
    # The line discipline echoes it, so a faithful .raw MUST contain it: this is
    # the pin against a future change stripping echo in the name of "purity".
    assert b"SECRETKEYSTROKE" in raw.read_bytes()


@_needs_pty
def test_a_raw_mode_session_journals_neither_sent_nor_reply_bytes(tmp_path: Path) -> None:
    raw = tmp_path / "pty-1.raw"
    with TerminalSession(
        [sys.executable, "-c", _RAW_MODE_CHILD], journal=Journal(tmp_path, 1),
    ) as s:
        assert _poll(lambda: b"CPR=" in raw.read_bytes(), timeout=_JOURNAL_POLL)
        s.send(b"SECRETKEYSTROKE")
        assert _poll(lambda: b"TYPED=" in raw.read_bytes(), timeout=_JOURNAL_POLL)
    data = raw.read_bytes()
    assert b"CPR=0" not in data  # pyte really did answer the query
    assert b"SECRETKEYSTROKE" not in data
    assert re.search(rb"\x1b\[\d+;\d+R", data) is None


@_needs_pty
def test_resize_appends_the_new_geometry(tmp_path: Path) -> None:
    with TerminalSession(
        [sys.executable, "-c", _SLEEPER], cols=80, rows=24, journal=Journal(tmp_path, 1),
    ) as s:
        s.resize(40, 120)
    resizes = [r for r in _records(tmp_path / "pty-1.jsonl") if r["ev"] == "resize"]
    assert [(r["cols"], r["rows"]) for r in resizes] == [(120, 40)]


@_needs_pty
def test_a_record_offset_counts_the_output_bytes_journaled_before_it(
    tmp_path: Path,
) -> None:
    raw = tmp_path / "pty-1.raw"
    with TerminalSession(
        [sys.executable, "-c", _HUNDRED_CHILD], journal=Journal(tmp_path, 1),
    ) as s:
        assert _poll(lambda: raw.stat().st_size == 100, timeout=_JOURNAL_POLL)
        s.resize(40, 120)
    recs = _records(tmp_path / "pty-1.jsonl")
    assert recs[0]["off"] == 0  # start, before any output
    assert next(r for r in recs if r["ev"] == "resize")["off"] == 100


@_needs_pty
def test_close_appends_exactly_one_end_record_with_the_exit_code(tmp_path: Path) -> None:
    sidecar = tmp_path / "pty-1.jsonl"
    s = TerminalSession([sys.executable, "-c", _SLEEPER], journal=Journal(tmp_path, 1))
    code = s.close()
    assert code < 0  # signal death from close()'s stop ladder
    recs = _records(sidecar)
    assert recs[-1]["ev"] == "end"
    assert recs[-1]["exit"] == code
    assert s.close() == code
    assert [r["ev"] for r in _records(sidecar)].count("end") == 1


@_needs_pty
def test_a_session_without_a_journal_writes_nothing(tmp_path: Path) -> None:
    with TerminalSession([sys.executable, "-c", _NEUTRAL_CHILD], journal=None) as s:
        s.run_to_completion(hard=30.0)
        assert "ALPHA-BETA-GAMMA" in "\n".join(s.lines())
    assert list(tmp_path.iterdir()) == []
