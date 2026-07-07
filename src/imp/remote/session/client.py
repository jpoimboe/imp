from __future__ import annotations

from collections.abc import Callable
from typing import Any

import anyio
from anyio.abc import SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.session.protocol import (
    _MAX_FRAME,
    encode_attach,
    encode_control,
    encode_input,
    encode_set_lease,
    parse_event,
)


class SessionClient:
    def __init__(
        self,
        path: str,
        change_id: str,
        title: str,
        *,
        on_event: Callable[[dict[str, Any]], None],
        lease: bool = False,
    ) -> None:
        self._path = path
        self._change_id = change_id
        self._title = title
        self._on_event = on_event
        self._lease = lease
        self._stream: SocketStream | None = None
        self._send_lock = anyio.Lock()

    async def connect(self) -> None:
        stream = await anyio.connect_unix(self._path)
        try:
            await stream.send(
                (encode_attach(self._change_id, self._title, self._lease) + "\n").encode("utf-8"))
        except BaseException:
            await stream.aclose()
            raise
        self._stream = stream

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

    async def send_input(self, text: str) -> None:
        async with self._send_lock:
            if self._stream is None:
                raise RuntimeError("call connect() before send_input()")
            await self._stream.send((encode_input(text) + "\n").encode("utf-8"))

    async def send_control(self, action: str, reason: str | None = None) -> None:
        async with self._send_lock:
            if self._stream is None:
                raise RuntimeError("call connect() before send_control()")
            await self._stream.send(
                (encode_control(action, reason) + "\n").encode("utf-8"))

    async def send_set_lease(self, value: bool) -> None:
        async with self._send_lock:
            if self._stream is None:
                raise RuntimeError("call connect() before send_set_lease()")
            await self._stream.send((encode_set_lease(value) + "\n").encode("utf-8"))

    async def aclose(self) -> None:
        async with self._send_lock:
            if self._stream is not None:
                await self._stream.aclose()
                self._stream = None
