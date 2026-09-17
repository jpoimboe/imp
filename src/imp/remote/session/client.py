# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
from __future__ import annotations

import contextlib
import enum
import socket
from collections.abc import Callable
from typing import Any

import anyio
from anyio.abc import SocketAttribute, SocketStream
from anyio.streams.buffered import BufferedByteReceiveStream

from imp.remote.session.protocol import (
    _MAX_FRAME,
    EndOfRun,
    encode_attach,
    encode_control,
    encode_input,
    encode_set_lease,
    encode_skill_request,
    parse_stream_frame,
)


class StreamEnding(enum.Enum):
    """Whether the receive loop saw the daemon's end-of-run frame, or never did."""
    ENDED = enum.auto()
    CUT = enum.auto()


class SessionClient:
    def __init__(
        self,
        path: str,
        quest_id: str,
        title: str,
        *,
        on_event: Callable[[dict[str, Any]], None],
        lease: bool = False,
    ) -> None:
        self._path = path
        self._quest_id = quest_id
        self._title = title
        self._on_event = on_event
        self._lease = lease
        self._stream: SocketStream | None = None
        self._send_lock = anyio.Lock()

    async def connect(self) -> None:
        stream = await anyio.connect_unix(self._path)
        try:
            await stream.send(
                (encode_attach(self._quest_id, self._title, self._lease) + "\n").encode("utf-8"))
        except BaseException:
            await stream.aclose()
            raise
        self._stream = stream

    async def receive_events(self) -> StreamEnding:
        """ENDED means the end frame arrived; the socket stays open — the caller must close it."""
        if self._stream is None:
            raise RuntimeError("call connect() before receive_events()")
        buf = BufferedByteReceiveStream(self._stream)
        while True:
            try:
                line = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
                frame = parse_stream_frame(line)
            except (anyio.EndOfStream, anyio.IncompleteRead,
                    anyio.ClosedResourceError, anyio.BrokenResourceError,
                    anyio.DelimiterNotFound, ValueError):
                return StreamEnding.CUT
            if isinstance(frame, EndOfRun):
                return StreamEnding.ENDED
            # Outside the try: a renderer bug must surface as a traceback, not as EOS.
            self._on_event(frame)

    async def send_input(self, text: str) -> None:
        async with self._send_lock:
            if self._stream is None:
                raise RuntimeError("call connect() before send_input()")
            await self._stream.send((encode_input(text) + "\n").encode("utf-8"))

    async def send_skill_request(self, name: str, args: str) -> None:
        async with self._send_lock:
            if self._stream is None:
                raise RuntimeError("call connect() before send_skill_request()")
            await self._stream.send((encode_skill_request(name, args) + "\n").encode("utf-8"))

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

    def shutdown_socket(self) -> None:
        """Shutdown(2) is sync-safe from any thread; aclose() needs the possibly-frozen loop."""
        stream = self._stream
        if stream is None:
            return
        with contextlib.suppress(OSError):
            stream.extra(SocketAttribute.raw_socket).shutdown(socket.SHUT_RDWR)

    async def aclose(self) -> None:
        async with self._send_lock:
            if self._stream is not None:
                await self._stream.aclose()
                self._stream = None
