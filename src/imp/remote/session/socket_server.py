"""SessionSocket — the per-change Unix-socket endpoint of the attach protocol.

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

from imp.remote.session.protocol import (
    _MAX_FRAME,
    Attach,
    Control,
    Input,
    encode_event,
    parse_client_message,
)

_CLIENT_BUFFER = 256  # per-client send-stream depth; overflow drops that one client
_log = logging.getLogger("imp.remote.session.socket_server")


@dataclass
class ReplaySnapshot:
    """On-disk event prefix re-framed for the wire; capture() must be called synchronously."""
    payload: bytes

    @classmethod
    def capture(cls, events_path: Path) -> ReplaySnapshot:
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
                    framed.append((encode_event(record) + "\n").encode("utf-8"))
        except OSError:
            _log.warning(
                "replay: cannot read %s; client gets the live stream only",
                events_path)
            return cls(payload=b"")
        return cls(payload=b"".join(framed))

    def wire_bytes(self) -> bytes:
        return self.payload


@dataclass(eq=False)  # eq=False keeps identity hash/eq so _Client is set-storable
class _Client:
    stream: Any
    send: MemoryObjectSendStream[dict[str, Any]]
    scope: Any


class SessionSocket:
    def __init__(
        self,
        path: str,
        *,
        on_attach: Callable[
            [Attach, Callable[[dict[str, Any]], None]], ReplaySnapshot | None
        ] | None = None,
        on_detach: Callable[[], None] | None = None,
        on_control: Callable[[str], None] | None = None,
        client_buffer: int = _CLIENT_BUFFER,
    ) -> None:
        self._path = path
        self._on_attach = on_attach
        self._on_detach = on_detach
        self._on_control = on_control
        self._client_buffer = client_buffer
        self._clients: set[_Client] = set()
        self._listener: SocketListener | None = None
        self._input_send: MemoryObjectSendStream[str] | None = None
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
        # MUST NOT pre-unlink self._path before binding. A non-pre-unlink bind is
        # what makes the concurrent double-spawn race safe: the second binder fails
        # with EADDRINUSE instead of stealing the live socket. A stale .sock is
        # handled on teardown, not by pre-unlinking here.
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

    def broadcast(self, event: dict[str, Any]) -> None:
        for client in list(self._clients):
            self._send_one(client, event)

    def _send_one(self, client: _Client, event: dict[str, Any]) -> None:
        try:
            client.send.send_nowait(event)
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
        self, client: _Client, recv: MemoryObjectReceiveStream[dict[str, Any]],
    ) -> None:
        with contextlib.suppress(anyio.ClosedResourceError, anyio.BrokenResourceError):
            async for event in recv:
                line = (encode_event(event) + "\n").encode("utf-8")
                try:
                    await client.stream.send(line)
                except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                    self._drop(client)
                    break

    async def next_input(self) -> str:
        if self._input_send is not None:
            raise RuntimeError("next_input called while gate is already open")
        # capacity-1 stream + WouldBlock drop: first write wins, loser not queued for next gate
        send, recv = anyio.create_memory_object_stream[str](max_buffer_size=1)
        self._input_send = send
        try:
            return await recv.receive()
        finally:
            self._input_send = None
            send.close()
            recv.close()

    def _dispatch(self, message: object) -> None:
        if isinstance(message, Input) and self._input_send is not None:
            with contextlib.suppress(anyio.WouldBlock):
                self._input_send.send_nowait(message.text)
        elif isinstance(message, Control) and self._on_control is not None:
            self._on_control(message.action)

    async def _handle(self, stream: SocketStream) -> None:
        buf = BufferedByteReceiveStream(stream)
        client: _Client | None = None
        try:
            first = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
            hello = parse_client_message(first)
            if not isinstance(hello, Attach):
                return
            send, recv = anyio.create_memory_object_stream[dict[str, Any]](
                max_buffer_size=self._client_buffer)
            async with anyio.create_task_group() as ctg:
                client = _Client(stream=stream, send=send, scope=ctg.cancel_scope)
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
                            self._dispatch(parse_client_message(line))
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
