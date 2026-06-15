"""SessionSocket — the per-change Unix-socket endpoint of the attach protocol.

Inverts SouthHub's direction: it BROADCASTS curated events to every attached
client and reads input/control frames back. Accepts N concurrent clients; a
dropped socket is a detach, never an error. The input gate (first-input-wins) and
control dispatch are added in Task 3."""
from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import Any

import anyio
from anyio.abc import SocketListener, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream
from anyio.streams.memory import MemoryObjectSendStream

from imp.remote.session.protocol import (
    Attach,
    Control,
    Input,
    encode_event,
    parse_client_message,
)

_MAX_FRAME = 1 << 20  # exceeding this raises DelimiterNotFound and silently detaches the client


class SessionSocket:
    def __init__(
        self,
        path: str,
        *,
        on_attach: Callable[[Attach], None] | None = None,
        on_detach: Callable[[], None] | None = None,
        on_control: Callable[[str], None] | None = None,
    ) -> None:
        self._path = path
        self._on_attach = on_attach
        self._on_detach = on_detach
        self._on_control = on_control
        self._clients: set[SocketStream] = set()
        self._listener: SocketListener | None = None
        self._input_send: MemoryObjectSendStream[str] | None = None

    async def open(self) -> None:
        self._listener = await anyio.create_unix_listener(self._path)

    async def serve(self) -> None:
        assert self._listener is not None
        with contextlib.suppress(anyio.ClosedResourceError):
            await self._listener.serve(self._handle)

    @property
    def attached_count(self) -> int:
        return len(self._clients)

    async def broadcast(self, event: dict[str, Any]) -> None:
        line = (encode_event(event) + "\n").encode("utf-8")
        for stream in list(self._clients):
            with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                await stream.send(line)

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
        attached = False
        buf = BufferedByteReceiveStream(stream)
        try:
            first = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
            hello = parse_client_message(first)
            if not isinstance(hello, Attach):
                return
            self._clients.add(stream)
            attached = True
            if self._on_attach is not None:
                self._on_attach(hello)
            while True:
                line = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
                with contextlib.suppress(ValueError, KeyError):
                    self._dispatch(parse_client_message(line))
        except (anyio.IncompleteRead, anyio.EndOfStream,
                anyio.ClosedResourceError, anyio.DelimiterNotFound,
                anyio.BrokenResourceError, ValueError, KeyError):
            pass
        finally:
            if attached:
                self._clients.discard(stream)
                if self._on_detach is not None:
                    self._on_detach()
            await stream.aclose()

    async def close(self) -> None:
        for stream in list(self._clients):
            with contextlib.suppress(anyio.BrokenResourceError, anyio.ClosedResourceError):
                await stream.aclose()
        if self._listener is not None:
            await self._listener.aclose()
            self._listener = None
