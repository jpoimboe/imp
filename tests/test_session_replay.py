from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.session.protocol import encode_attach, parse_event
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


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _attach(sock_path: str, change_id: str) -> SocketStream:
    client = await anyio.connect_unix(sock_path)
    await client.send((encode_attach(change_id, "T") + "\n").encode())
    return client


@pytest.mark.anyio
async def test_late_attach_replays_full_disk_prefix_then_live_tail(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    sock = str(tmp_path / "s.sock")
    # disk holds an un-broadcast leading line (change_created) plus two lines that
    # the run also broadcasts. A broadcast-counter partition would drop "thinking";
    # the disk-line partition replays all three.
    prefix = [
        {"id": "e0", "type": "change_created", "payload": {"id": "x"}},
        {"id": "e1", "type": "step_started", "payload": {"step": "spec"}},
        {"id": "e2", "type": "thinking", "payload": {"content": "hmm"}},
    ]
    _write_jsonl(events_path, prefix)
    emitted = anyio.Event()
    attached = anyio.Event()
    server = SessionServer(sock, events_path=events_path)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        for e in prefix[1:]:          # e1,e2 broadcast before attach: lost to the client
            on_event(e)
        emitted.set()
        await attached.wait()
        on_event({"id": "e3", "type": "step_completed",
                  "payload": {"step": "spec", "action": {"verb": "continue"}}})
        await anyio.sleep(0.1)
        return "ok"

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)
        await emitted.wait()
        client = await _attach(sock, "c1")
        buf = BufferedByteReceiveStream(client)
        seen: list[dict[str, Any]] = []
        with anyio.fail_after(5):
            while len(seen) < 3:
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert [s["id"] for s in seen] == ["e0", "e1", "e2"]   # full disk prefix, in order
        attached.set()
        with anyio.fail_after(5):
            seen.append(parse_event(
                (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert seen[-1]["id"] == "e3"                          # live tail across the seam
        assert [s["id"] for s in seen] == ["e0", "e1", "e2", "e3"]  # each once, no gap/dup
        await client.aclose()
        tg.cancel_scope.cancel()


@pytest.mark.anyio
async def test_resume_attach_replays_entire_prior_journal(tmp_path: Path) -> None:
    events_path = tmp_path / "events.jsonl"
    sock = str(tmp_path / "s.sock")
    # Simulate a resumed run: disk already holds a long prior journal + the
    # direct-to-disk workflow_resumed, while the new process's run emits fresh
    # events. A late attach must replay the WHOLE prior journal, not the first few.
    prior = [{"id": f"p{i}", "type": "thinking", "payload": {"content": str(i)}}
             for i in range(20)]
    prior.append({"id": "wr", "type": "workflow_resumed", "payload": {}})
    _write_jsonl(events_path, prior)
    emitted = anyio.Event()
    attached = anyio.Event()
    server = SessionServer(sock, events_path=events_path)

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        on_event({"id": "n0", "type": "step_started", "payload": {"step": "plan"}})
        emitted.set()
        await attached.wait()
        on_event({"id": "n1", "type": "thinking", "payload": {"content": "live"}})
        await anyio.sleep(0.1)
        return "ok"

    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)
        await emitted.wait()
        client = await _attach(sock, "c1")
        buf = BufferedByteReceiveStream(client)
        seen: list[dict[str, Any]] = []
        with anyio.fail_after(5):
            while len(seen) < 21:
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert [s["id"] for s in seen] == [f"p{i}" for i in range(20)] + ["wr"]
        attached.set()
        with anyio.fail_after(5):
            seen.append(parse_event(
                (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert seen[-1]["id"] == "n1"
        assert [s["id"] for s in seen] == [f"p{i}" for i in range(20)] + ["wr", "n1"]
        await client.aclose()
        tg.cancel_scope.cancel()
