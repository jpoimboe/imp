# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""SessionSocket — the per-quest Unix-socket endpoint of the attach protocol.

Inverts SouthHub's direction: it BROADCASTS curated events to every attached
client and reads input/control frames back. Accepts N concurrent clients; a
dropped socket is a detach, never an error. The input gate (first-input-wins) and
control dispatch are added in Task 3."""
from __future__ import annotations

import contextlib
import json
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import anyio
from anyio.abc import SocketListener, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from imp.agents.transcript import ContinuationNote
from imp.remote.session.protocol import (
    _MAX_FRAME,
    END_OF_RUN,
    Attach,
    Control,
    EndOfRun,
    Input,
    Query,
    SetLease,
    SkillRequest,
    encode_end,
    encode_event,
    encode_result,
    encode_result_error,
    parse_client_message,
)

# What the input channel carries: a client reply, a client skill request, or a server-injected note.
InputPayload = str | SkillRequest | ContinuationNote

PumpItem = dict[str, Any] | EndOfRun

_CLIENT_BUFFER = 256  # per-client send-stream depth; overflow drops that one client
_FLUSH_TIMEOUT = 5.0   # teardown backstop: a dead client can't wedge the flush
_log = logging.getLogger("imp.remote.session.socket_server")


@dataclass
class ReplaySnapshot:
    """On-disk event prefix re-framed for the wire; capture() must be called synchronously."""
    payload: bytes

    @classmethod
    def capture(cls, events_path: Path) -> ReplaySnapshot:
        # Transport-level end-of-replay boundary; final frame on every path, never persisted.
        boundary = (encode_event({"type": "replay_complete"}) + "\n").encode("utf-8")
        framed: list[bytes] = []
        try:
            with events_path.open("rb") as f:
                for raw in f:
                    if not raw.endswith(b"\n"):
                        break  # partial trailing line: writer mid-append; skip it
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        _log.warning("replay: corrupt line in %s; truncating replay", events_path)
                        break
                    framed.append(
                        (encode_event({**record, "_replay": True}) + "\n").encode("utf-8"))
        except OSError:
            _log.warning(
                "replay: cannot read %s; client gets the live stream only",
                events_path)
            return cls(payload=boundary)
        framed.append(boundary)
        return cls(payload=b"".join(framed))

    def wire_bytes(self) -> bytes:
        return self.payload


@dataclass(eq=False)  # eq=False keeps identity hash/eq so _Client is set-storable
class _Client:
    stream: Any
    send: MemoryObjectSendStream[PumpItem]
    scope: Any
    leased: bool = False
    # Set on pump exit; teardown flush awaits it. None until wired in _handle (needs a loop).
    pump_done: anyio.Event | None = None


class SessionSocket:
    def __init__(
        self,
        path: str,
        *,
        on_attach: Callable[
            [Attach, Callable[[dict[str, Any]], None]], ReplaySnapshot | None
        ] | None = None,
        on_detach: Callable[[], None] | None = None,
        on_control: Callable[[str, str | None], None] | None = None,
        on_lease_change: Callable[[], None] | None = None,
        on_query: Callable[[str, dict[str, Any]], Any] | None = None,
        client_buffer: int = _CLIENT_BUFFER,
    ) -> None:
        self._path = path
        self._on_attach = on_attach
        self._on_detach = on_detach
        self._on_control = on_control
        self._on_lease_change = on_lease_change
        self._on_query = on_query
        self._client_buffer = client_buffer
        self._clients: set[_Client] = set()
        self._listener: SocketListener | None = None
        self._input_send: MemoryObjectSendStream[InputPayload] | None = None
        # Single-slot latch for a reply that arrives after a solicit armed but
        # before next_input() opened the gate. Armed only while soliciting.
        self._pending_input: InputPayload | None = None
        self._input_armed: bool = False
        self._serve_scope: anyio.CancelScope | None = None
        self._serve_done: anyio.Event | None = None

    async def open(self) -> None:
        parent = Path(self._path).parent
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        st = parent.stat()
        my_uid = os.geteuid()
        if st.st_uid != my_uid:
            raise PermissionError(
                f"refusing to use session-socket dir {parent}: owned by uid {st.st_uid}, "
                f"not {my_uid}")
        # mkdir's mode= is ignored on existing dirs; enforce it explicitly.
        if st.st_mode & 0o777 != 0o700:
            parent.chmod(0o700)
        # anyio.create_unix_listener pre-unlinks any existing socket file before
        # binding, so a second binder would replace the path, not get EADDRINUSE.
        # The concurrent double-spawn race is instead made safe by the per-quest
        # ProjectLock (daemon.py serve_quest_session): a racing spawn raises
        # LockHeld and exits without binding. The stale .sock is removed by
        # close() on teardown, so no pre-unlink is needed here.
        self._listener = await anyio.create_unix_listener(self._path)

    async def serve(self) -> None:
        if self._listener is None:
            return
        done = anyio.Event()
        self._serve_done = done
        try:
            with anyio.CancelScope() as scope:
                self._serve_scope = scope
                with contextlib.suppress(anyio.ClosedResourceError):
                    await self._listener.serve(self._handle)
        finally:
            self._serve_scope = None
            self._serve_done = None
            done.set()

    @property
    def attached_count(self) -> int:
        return len(self._clients)

    @property
    def leased_count(self) -> int:
        return sum(1 for c in self._clients if c.leased)

    def broadcast(self, event: dict[str, Any]) -> None:
        for client in list(self._clients):
            self._send_one(client, event)

    def enqueue_end(self) -> None:
        """Queue the end-of-run marker behind every pending event, for every client."""
        for client in list(self._clients):
            self._send_one(client, END_OF_RUN)

    def _send_one(self, client: _Client, item: PumpItem) -> None:
        try:
            client.send.send_nowait(item)
        except anyio.WouldBlock:
            self._drop(client)
        except (anyio.ClosedResourceError, anyio.BrokenResourceError):
            self._clients.discard(client)

    def _drop(self, client: _Client) -> None:
        self._clients.discard(client)
        with contextlib.suppress(anyio.ClosedResourceError):
            client.send.close()
        client.scope.cancel()

    async def _pump_client(
        self, client: _Client, recv: MemoryObjectReceiveStream[PumpItem],
    ) -> None:
        try:
            with contextlib.suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
                async for first in recv:
                    # Coalesce all currently-buffered items into one write: at run
                    # teardown the pump is cancelled right after run_fn returns, so a
                    # per-item await would flush only the first queued one and drop
                    # the rest. Draining synchronously into one send closes that window.
                    batch: list[PumpItem] = [first]
                    while True:
                        try:
                            batch.append(recv.receive_nowait())
                        except (anyio.WouldBlock, anyio.EndOfStream):
                            break
                    line = b"".join(
                        ((encode_end() if isinstance(item, EndOfRun)
                          else encode_event(item)) + "\n").encode("utf-8")
                        for item in batch)
                    try:
                        await client.stream.send(line)
                    except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                        self._drop(client)
                        break
        finally:
            if client.pump_done is not None:
                client.pump_done.set()

    async def flush_pending(self) -> None:
        """Drain queued client events before teardown; bounded against a dead client."""
        clients = list(self._clients)
        for client in clients:
            with contextlib.suppress(anyio.ClosedResourceError):
                client.send.close()
        for client in clients:
            if client.pump_done is not None:
                with anyio.move_on_after(_FLUSH_TIMEOUT):
                    await client.pump_done.wait()

    def arm_input(self) -> None:
        """Latch a reply arriving before the gate opens. Pair with disarm_input()."""
        self._input_armed = True

    def disarm_input(self) -> None:
        """Stop expecting a reply; drop any unconsumed latched frame so it can't leak later."""
        self._input_armed = False
        self._pending_input = None

    async def next_input(self) -> InputPayload:
        if self._input_send is not None:
            raise RuntimeError("next_input called while gate is already open")
        # capacity-1 stream + WouldBlock drop: first write wins, loser not queued for next gate
        send, recv = anyio.create_memory_object_stream[InputPayload](max_buffer_size=1)
        self._input_send = send
        if self._pending_input is not None:
            # A reply latched between arm and gate-open; deliver it to this gate.
            send.send_nowait(self._pending_input)
            self._pending_input = None
        try:
            return await recv.receive()
        finally:
            self._input_send = None
            send.close()
            recv.close()

    def inject_input(self, payload: InputPayload) -> None:
        """Deliver a payload to the open gate, else latch it if a solicit is armed, else drop it."""
        if self._input_send is not None:
            with contextlib.suppress(anyio.WouldBlock):
                self._input_send.send_nowait(payload)
        elif self._input_armed and self._pending_input is None:
            # Latch the first pre-gate reply (first-wins, like the open gate).
            self._pending_input = payload

    def _dispatch(self, client: _Client, message: object) -> None:
        if isinstance(message, (Input, SkillRequest)):
            self.inject_input(message.text if isinstance(message, Input) else message)
        elif isinstance(message, Control) and self._on_control is not None:
            self._on_control(message.action, message.reason)
        elif isinstance(message, SetLease):
            client.leased = message.value
            if self._on_lease_change is not None:
                self._on_lease_change()

    async def _handle(self, stream: SocketStream) -> None:
        buf = BufferedByteReceiveStream(stream)
        client: _Client | None = None
        try:
            first = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
            hello = parse_client_message(first)
            if isinstance(hello, Query):
                await self._serve_query(stream, hello)
                return
            if not isinstance(hello, Attach):
                return
            send, recv = anyio.create_memory_object_stream[PumpItem](
                max_buffer_size=self._client_buffer)
            async with anyio.create_task_group() as ctg:
                client = _Client(
                    stream=stream, send=send, scope=ctg.cancel_scope,
                    leased=hello.lease)
                client.pump_done = anyio.Event()
                self._clients.add(client)
                snapshot: ReplaySnapshot | None = None
                if self._on_attach is not None:
                    snapshot = self._on_attach(
                        hello, lambda ev: self._send_one(client, ev))
                if snapshot is not None:
                    raw = snapshot.wire_bytes()
                    if raw:
                        with contextlib.suppress(anyio.BrokenResourceError,
                                                 anyio.ClosedResourceError):
                            await stream.send(raw)
                ctg.start_soon(self._pump_client, client, recv)
                try:
                    while True:
                        line = (await buf.receive_until(
                            b"\n", _MAX_FRAME)).decode("utf-8")
                        with contextlib.suppress(ValueError, KeyError):
                            self._dispatch(client, parse_client_message(line))
                except (anyio.IncompleteRead, anyio.EndOfStream,
                        anyio.ClosedResourceError, anyio.DelimiterNotFound,
                        anyio.BrokenResourceError, ValueError, KeyError):
                    pass
                finally:
                    ctg.cancel_scope.cancel()
        except (anyio.IncompleteRead, anyio.EndOfStream,
                anyio.ClosedResourceError, anyio.DelimiterNotFound,
                anyio.BrokenResourceError, ValueError, KeyError):
            pass
        finally:
            if client is not None:
                self._clients.discard(client)
                with contextlib.suppress(anyio.ClosedResourceError):
                    client.send.close()
                if self._on_detach is not None:
                    self._on_detach()
            await stream.aclose()

    async def _serve_query(self, stream: SocketStream, query: Query) -> None:
        if self._on_query is None:
            return
        try:
            value = await self._on_query(query.op, query.args)
            frame = encode_result(value)
        except Exception as exc:  # RPC boundary: report to caller, never crash the server
            _log.warning("query op %r failed: %s", query.op, exc)
            frame = encode_result_error(str(exc))
        with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
            await stream.send((frame + "\n").encode("utf-8"))

    async def detach_all(self) -> None:
        for client in list(self._clients):
            with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                await client.stream.aclose()

    async def close(self) -> None:
        await self.detach_all()
        # Cancel before close: keeps accept()'s done callback fd-valid,
        # preventing stale selector entry on reuse.
        if self._serve_scope is not None:
            done = self._serve_done
            self._serve_scope.cancel()
            if done is not None:
                await done.wait()
        if self._listener is not None:
            await self._listener.aclose()
            self._listener = None
        # aclose() only closes the fd; unlink the pathname socket so clean
        # teardown does not leak a stale .sock. Unconditional (every close path)
        # and idempotent so a second close / already-rmtree'd dir is safe.
        Path(self._path).unlink(missing_ok=True)
