from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import anyio
import pytest
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.session.protocol import _MAX_FRAME, encode_attach, parse_event
from imp.remote.session.server import SessionServer
from imp.remote.session.socket_server import AttachRefused, ReplaySnapshot
from tests.async_wait import HANG_BACKSTOP


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_capture_frames_every_complete_line(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    recs = [{"id": str(i), "type": "x", "payload": {"i": i}} for i in range(5)]
    _write_jsonl(p, recs)
    lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    events = [parse_event(ln) for ln in lines]
    assert events[:-1] == [{**r, "_replay": True} for r in recs]
    assert events[-1] == {"type": "replay_complete"}


def test_capture_includes_lines_that_never_broadcast(tmp_path: Path) -> None:
    # quest_created / workflow_resumed land on disk without a broadcast. Capture
    # reads disk, so it must replay them — this is exactly what a broadcast counter
    # would have dropped at the seam.
    p = tmp_path / "events.jsonl"
    recs = [
        {"id": "c", "type": "quest_created", "payload": {"id": "x"}},
        {"id": "r", "type": "workflow_resumed", "payload": {}},
        {"id": "s", "type": "step_started", "payload": {"step": "spec"}},
    ]
    _write_jsonl(p, recs)
    lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    assert [parse_event(ln)["type"] for ln in lines] == [
        "quest_created", "workflow_resumed", "step_started", "replay_complete"]


def test_capture_ignores_partial_trailing_line(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    good = json.dumps({"id": "a", "type": "x", "payload": {}})
    p.write_text(good + "\n" + '{"id":"b","type":"part')  # partial, no newline
    lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    events = [parse_event(ln) for ln in lines]
    assert events[:-1] == [{**json.loads(good), "_replay": True}]
    assert events[-1] == {"type": "replay_complete"}


def test_capture_appends_replay_complete_boundary_as_final_frame(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    recs = [{"id": str(i), "type": "x", "payload": {"i": i}} for i in range(3)]
    _write_jsonl(p, recs)
    lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    events = [parse_event(ln) for ln in lines]
    # every replayed record is framed first, marked _replay...
    assert events[:-1] == [{**r, "_replay": True} for r in recs]
    # ...and the single trailing frame is the boundary (no _replay key)
    assert events[-1] == {"type": "replay_complete"}


def test_capture_empty_log_still_emits_boundary(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    p.write_text("")
    lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    assert [parse_event(ln) for ln in lines] == [{"type": "replay_complete"}]


def test_capture_unreadable_log_still_emits_boundary(
    tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    p = tmp_path / "nope.jsonl"
    p.mkdir()  # exists but unreadable as a file; chmod 000 is bypassed under root
    with caplog.at_level(logging.WARNING):
        lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    assert [parse_event(ln) for ln in lines] == [{"type": "replay_complete"}]
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
    events = [parse_event(ln) for ln in lines]
    assert events[:-1] == [{**r, "_replay": True} for r in recs]
    assert events[-1] == {"type": "replay_complete"}


def test_on_attach_pulse_without_events_path_returns_none(tmp_path: Path) -> None:
    server = SessionServer(str(tmp_path / "s.sock"))
    assert server._on_attach_pulse(None, lambda _e: None) is None


def test_on_attach_pulse_refuses_once_the_log_is_gone(tmp_path: Path) -> None:
    server = SessionServer(str(tmp_path / "s.sock"), events_path=tmp_path / "events.jsonl")
    server.set_pending_prompt(
        {"type": "awaiting_user_input", "payload": {"prompt": "q"}})
    sent: list[dict[str, Any]] = []
    assert server._on_attach_pulse(None, sent.append) is AttachRefused.HISTORY_GONE
    assert sent == []   # a refused client is not re-sent the pending prompt


def test_on_attach_pulse_serves_only_the_boundary_for_an_unreadable_log(
    tmp_path: Path,
) -> None:
    p = tmp_path / "events.jsonl"
    server = SessionServer(str(tmp_path / "s.sock"), events_path=p)
    p.mkdir()  # exists but unreadable as a file: not "history gone"
    snap = server._on_attach_pulse(None, lambda _e: None)
    assert isinstance(snap, ReplaySnapshot)
    assert [parse_event(ln) for ln in snap.wire_bytes().decode().splitlines()] == [
        {"type": "replay_complete"}]


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def _attach(sock_path: str, quest_id: str) -> SocketStream:
    client = await anyio.connect_unix(sock_path)
    await client.send((encode_attach(quest_id, "T") + "\n").encode())
    return client


@pytest.mark.anyio
async def test_late_attach_replays_full_disk_prefix_then_live_tail(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    sock = str(tmp_path / "s.sock")
    # disk holds an un-broadcast leading line (quest_created) plus two lines that
    # the run also broadcasts. A broadcast-counter partition would drop "thinking";
    # the disk-line partition replays all three.
    prefix = [
        {"id": "e0", "type": "quest_created", "payload": {"id": "x"}},
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
        with anyio.fail_after(30):  # deadlock backstop only
            while len(seen) < 3:
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert [s["id"] for s in seen] == ["e0", "e1", "e2"]   # full disk prefix, in order
        # boundary closes the replay
        with anyio.fail_after(30):  # deadlock backstop only
            boundary = parse_event(
                (await buf.receive_until(b"\n", 1 << 20)).decode())
        assert boundary == {"type": "replay_complete"}         # arrives before any live event
        attached.set()
        with anyio.fail_after(30):  # deadlock backstop only
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
        with anyio.fail_after(30):  # deadlock backstop only
            while len(seen) < 21:
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert [s["id"] for s in seen] == [f"p{i}" for i in range(20)] + ["wr"]
        # boundary closes the replay
        with anyio.fail_after(30):  # deadlock backstop only
            boundary = parse_event(
                (await buf.receive_until(b"\n", 1 << 20)).decode())
        assert boundary == {"type": "replay_complete"}         # arrives before any live event
        attached.set()
        with anyio.fail_after(30):  # deadlock backstop only
            seen.append(parse_event(
                (await buf.receive_until(b"\n", 1 << 20)).decode()))
        assert seen[-1]["id"] == "n1"
        assert [s["id"] for s in seen] == [f"p{i}" for i in range(20)] + ["wr", "n1"]
        await client.aclose()
        tg.cancel_scope.cancel()


def test_capture_marks_every_record_as_replay(tmp_path: Path) -> None:
    p = tmp_path / "events.jsonl"
    recs = [{"id": "a", "type": "awaiting_user_input", "payload": {"prompt": "q"}}]
    _write_jsonl(p, recs)
    lines = ReplaySnapshot.capture(p).wire_bytes().decode().splitlines()
    event = parse_event(lines[0])
    assert event["_replay"] is True
    assert event["type"] == "awaiting_user_input"
    assert event["payload"] == {"prompt": "q"}


def test_replay_marker_does_not_change_rendering() -> None:
    from imp.cli.narrative import build_tree
    from imp.cli.render_text import render_tree

    events = [
        {"type": "quest_created", "payload": {"id": "x", "title": "T"}},
        {"type": "step_started", "payload": {"step": "spec"}},
        {"type": "agent_result", "payload": {"text": "hi"}},
    ]
    marked = [{**e, "_replay": True} for e in events]
    plain = list(render_tree(build_tree(events), full=False, color=False, width=80))
    with_marker = list(render_tree(build_tree(marked), full=False, color=False, width=80))
    assert with_marker == plain


@pytest.mark.anyio
async def test_replay_of_an_oversized_event_stays_within_the_frame_cap(
    tmp_path: Path,
) -> None:
    events_path = tmp_path / "events.jsonl"
    sock = str(tmp_path / "s.sock")
    huge = "x" * (2 << 20)
    recs = [
        {"id": "e0", "type": "quest_created", "payload": {"id": "x"}},
        {"id": "e1", "type": "tool_call",
         "payload": {"id": "t1", "name": "write_file",
                     "input": {"path": "big.txt", "content": huge}}},
        {"id": "e2", "type": "step_started", "payload": {"step": "spec"}},
    ]
    _write_jsonl(events_path, recs)

    frames = ReplaySnapshot.capture(events_path).wire_bytes().split(b"\n")[:-1]
    assert len(frames) == 4                       # 3 records + the replay_complete boundary
    assert all(len(f) + 1 <= _MAX_FRAME for f in frames)

    done = anyio.Event()

    async def run_fn(*, on_event: Any, user_chat_input_fn: Any) -> str:
        await done.wait()
        return "ok"

    server = SessionServer(sock, events_path=events_path)
    async with anyio.create_task_group() as tg:
        await server.open()
        tg.start_soon(server.serve, run_fn)
        client = await _attach(sock, "c1")
        buf = BufferedByteReceiveStream(client)
        seen: list[dict[str, Any]] = []
        with anyio.fail_after(HANG_BACKSTOP):   # deadlock backstop only
            while len(seen) < 4:
                seen.append(parse_event(
                    (await buf.receive_until(b"\n", _MAX_FRAME)).decode()))
        # every replayed event reaches the client, oversized one included
        assert [s.get("id") for s in seen] == ["e0", "e1", "e2", None]
        assert seen[3] == {"type": "replay_complete"}
        assert seen[1]["payload"]["input"]["content"].endswith("… [clipped]")
        assert seen[1]["payload"]["input"]["path"] == "big.txt"
        done.set()
        await client.aclose()
        tg.cancel_scope.cancel()
