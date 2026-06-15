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

from imp.remote.session.protocol import Attach, encode_event, parse_client_message

_MAX_FRAME = 1 << 20


class SessionSocket:
    def __init__(
        self,
        path: str,
        *,
        on_attach: Callable[[Attach], None] | None = None,
        on_detach: Callable[[], None] | None = None,
    ) -> None:
        self._path = path
        self._on_attach = on_attach
        self._on_detach = on_detach
        self._clients: set[SocketStream] = set()
        self._listener: SocketListener | None = None

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
            try:
                await stream.send(line)
            except (anyio.BrokenResourceError, anyio.ClosedResourceError):
                pass

    async def _handle(self, stream: SocketStream) -> None:
        attached = False
        buf = BufferedByteReceiveStream(stream)
        try:
            first = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
            if not isinstance(parse_client_message(first), Attach):
                return
            self._clients.add(stream)
            attached = True
            if self._on_attach is not None:
                self._on_attach(parse_client_message(first))  # type: ignore[arg-type]
            while True:
                await buf.receive_until(b"\n", _MAX_FRAME)
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
        if self._listener is not None:
            await self._listener.aclose()
            self._listener = None
