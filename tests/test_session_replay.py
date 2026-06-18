from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest

from imp.remote.session.protocol import parse_event
from imp.remote.session.server import SessionServer
from imp.remote.session.socket_server import ReplaySnapshot


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_capture_frames_every_complete_line(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    recs = [{"id": str(i), "type": "x", "payload": {"i": i}} for i in range(5)]
    _write_jsonl(p, recs)
    lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    assert [parse_event(ln) for ln in lines] == recs


def test_capture_includes_lines_that_never_broadcast(tmp_path: Path) -> None:
    # change_created / workflow_resumed land on disk without a broadcast. Capture
    # reads disk, so it must replay them — this is exactly what a broadcast counter
    # would have dropped at the seam.
    p = tmp_path / "events.jsonl"
    recs = [
        {"id": "c", "type": "change_created", "payload": {"id": "x"}},
        {"id": "r", "type": "workflow_resumed", "payload": {}},
        {"id": "s", "type": "step_started", "payload": {"step": "spec"}},
    ]
    _write_jsonl(p, recs)
    lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    assert [parse_event(ln)["type"] for ln in lines] == [
        "change_created", "workflow_resumed", "step_started"]


def test_capture_empty_file_is_empty(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_text("")
    assert ReplaySnapshot.capture(p).wire_bytes() == b""


def test_capture_ignores_partial_trailing_line(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    good = json.dumps({"id": "a", "type": "x", "payload": {}})
    p.write_text(good + "\n" + '{"id":"b","type":"part')  # partial, no newline
    lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    assert [parse_event(ln) for ln in lines] == [json.loads(good)]


def test_capture_missing_file_yields_empty_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING):
        assert ReplaySnapshot.capture(tmp_path / "nope.jsonl").wire_bytes() == b""
    assert any("replay" in r.message.lower() for r in caplog.records)


def test_on_attach_pulse_returns_snapshot_of_current_disk(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    recs = [{"id": "a", "type": "x", "payload": {}},
            {"id": "b", "type": "y", "payload": {}}]
    _write_jsonl(p, recs)
    server = SessionServer(str(tmp_path / "s.sock"), events_path=p)
    sent: list[dict[str, Any]] = []
    snap = server._on_attach_pulse(None, sent.append)   # no open(): pulse is a no-op
    assert snap is not None
    lines = snap.wire_bytes().decode().splitlines()
    assert [parse_event(ln) for ln in lines] == recs


def test_on_attach_pulse_without_events_path_returns_none(tmp_path: Path) -> None:
    server = SessionServer(str(tmp_path / "s.sock"))
    assert server._on_attach_pulse(None, lambda _e: None) is None
