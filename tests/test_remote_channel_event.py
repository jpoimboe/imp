# tests/test_remote_channel_event.py
from __future__ import annotations

import json
import os

from imp.cli.remote_channel import RemoteChannel


class _FakeProc:
    """A live-process stand-in: a writable pipe fd and poll()->None (alive)."""

    def __init__(self, write_fd: int) -> None:
        self.stdin = _FakeStdin(write_fd)
        self._alive = True

    def poll(self) -> int | None:
        return None if self._alive else 1

    def die(self) -> None:
        self._alive = False


class _FakeStdin:
    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd


def _channel(write_fd: int, *, events: tuple[str, ...]) -> RemoteChannel:
    ch = RemoteChannel(
        command=("x",), events=events, startup_timeout=1.0,
        id="2026-06-11-abc", title="My Quest")
    ch._proc = _FakeProc(write_fd)  # inject a live fake process; _write_nonblocking sets O_NONBLOCK
    return ch


def test_on_event_writes_ndjson_with_quest_context() -> None:
    r, w = os.pipe()
    ch = _channel(w, events=("all",))
    ch.on_event({"id": "ev1", "type": "agent_output",
                 "payload": {"text": "hi"}})
    line = os.read(r, 65536).decode()
    assert line.endswith("\n")
    obj = json.loads(line)
    # The event's own fields are preserved (event id NOT clobbered)...
    assert obj["id"] == "ev1"
    assert obj["type"] == "agent_output"
    # ...and change context is namespaced under "change" (spec AC 2).
    assert obj["change"] == {
        "id": "2026-06-11-abc", "title": "My Quest",
        "step": None, "status": "running"}
    os.close(r)
    os.close(w)


def test_on_event_tracks_step_and_status() -> None:
    r, w = os.pipe()
    ch = _channel(w, events=("all",))
    ch.on_event({"id": "e0", "type": "step_started",
                 "payload": {"step": "plan", "kind": "agent"}})
    ch.on_event({"id": "e1", "type": "workflow_paused",
                 "payload": {"reason": "interrupt"}})
    # Both on_event calls complete synchronously, so both NDJSON lines land in
    # the pipe buffer before any reads. Drain all at once (non-blocking) and
    # parse the second line — which carries the accumulated tracking state.
    os.set_blocking(r, False)
    try:
        data = os.read(r, 65536)
    except BlockingIOError:
        data = b""
    lines = [ln for ln in data.split(b"\n") if ln]
    assert len(lines) == 2
    second = json.loads(lines[1])
    assert second["change"]["step"] == "plan"
    assert second["change"]["status"] == "paused"
    os.close(r)
    os.close(w)


def test_on_event_allowlist_drops_unlisted_type() -> None:
    r, w = os.pipe()
    ch = _channel(w, events=("awaiting_user_input",))
    ch.on_event({"id": "e", "type": "tool_call", "payload": {}})
    os.set_blocking(r, False)
    try:
        data = os.read(r, 65536)
    except BlockingIOError:
        data = b""
    assert data == b""   # dropped: not on the allowlist
    os.close(r)
    os.close(w)


def test_on_event_dead_proc_never_raises() -> None:
    r, w = os.pipe()
    ch = _channel(w, events=("all",))
    ch._proc.die()
    ch.on_event({"id": "e", "type": "agent_output", "payload": {}})  # no raise
    os.close(r)
    os.close(w)


def test_on_event_full_pipe_is_dropped_not_blocked() -> None:
    r, w = os.pipe()
    ch = _channel(w, events=("all",))
    # Fill the pipe buffer so the next os.write would block on a blocking fd;
    # the channel's fd is non-blocking, so the write must be dropped, not hang.
    big = {"id": "e", "type": "agent_output", "payload": {"x": "z" * 4096}}
    for _ in range(512):
        ch.on_event(big)   # must return promptly, never block or raise
    os.close(r)
    os.close(w)
