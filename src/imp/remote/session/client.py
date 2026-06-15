from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.session.protocol import encode_attach, parse_event

_MAX_FRAME = 1 << 20


class SessionClient:
    def __init__(
        self,
        path: str,
        change_id: str,
        title: str,
        *,
        on_event: Callable[[dict[str, Any]], None],
    ) -> None:
        self._path = path
        self._change_id = change_id
        self._title = title
        self._on_event = on_event
        self._stream: SocketStream | None = None

    async def connect(self) -> None:
        self._stream = await anyio.connect_unix(self._path)
        await self._stream.send(
            (encode_attach(self._change_id, self._title) + "\n").encode("utf-8"))

    async def receive_events(self) -> None:
        if self._stream is None:
            raise RuntimeError("call connect() before receive_events()")
        buf = BufferedByteReceiveStream(self._stream)
        try:
            while True:
                line = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
                self._on_event(parse_event(line))
        except (anyio.EndOfStream, anyio.IncompleteRead,
                anyio.ClosedResourceError, anyio.BrokenResourceError,
                anyio.DelimiterNotFound, ValueError):
            pass

    async def aclose(self) -> None:
        if self._stream is not None:
            await self._stream.aclose()
            self._stream = None
