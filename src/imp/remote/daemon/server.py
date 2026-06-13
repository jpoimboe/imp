"""SouthHub — the per-repo Unix-socket multiplexer for live Change connections."""
from __future__ import annotations

import contextlib
import json
from collections.abc import Callable

import anyio
from anyio.abc import SocketListener, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.daemon.protocol import parse_hello

_MAX_FRAME = 1 << 20


class SouthHub:
    """Owns the south-side socket and the live change_id -> stream registry."""

    def __init__(self, path: str, on_event: Callable[[str, str], None]) -> None:
        self._path = path
        self._on_event = on_event
        self._conns: dict[str, SocketStream] = {}
        self._listener: SocketListener | None = None

    async def open(self) -> None:
        self._listener = await anyio.create_unix_listener(self._path)

    async def serve(self) -> None:
        assert self._listener is not None
        with contextlib.suppress(anyio.ClosedResourceError):
            await self._listener.serve(self._handle)

    def live_ids(self) -> set[str]:
        return set(self._conns)

    async def _handle(self, stream: SocketStream) -> None:
        change_id: str | None = None
        buf = BufferedByteReceiveStream(stream)
        try:
            hello = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
            change_id, _title = parse_hello(hello)
            self._conns[change_id] = stream
            while True:
                line = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
                self._on_event(change_id, line)
        except (anyio.IncompleteRead, anyio.EndOfStream,
                anyio.ClosedResourceError, anyio.DelimiterNotFound,
                anyio.BrokenResourceError,
                json.JSONDecodeError, KeyError, ValueError):
            pass
        finally:
            if change_id is not None:
                self._conns.pop(change_id, None)
            await stream.aclose()

    async def send_reply(self, change_id: str, text: str) -> bool:
        """Push one reply line to the connection for change_id; return False if not live."""
        stream = self._conns.get(change_id)
        if stream is None:
            return False
        try:
            await stream.send((text + "\n").encode("utf-8"))
        except (anyio.BrokenResourceError, anyio.ClosedResourceError):
            return False
        return True

    async def close(self) -> None:
        if self._listener is not None:
            await self._listener.aclose()
            self._listener = None
