"""SouthHub — the per-repo Unix-socket multiplexer for live Quest connections."""
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
    """Owns the south-side socket and the live quest_id -> stream registry."""

    def __init__(
        self,
        path: str,
        on_event: Callable[[str, str], None],
        *,
        on_attach: Callable[[str, bool], None] | None = None,
        on_detach: Callable[[str], None] | None = None,
    ) -> None:
        self._path = path
        self._on_event = on_event
        self._on_attach = on_attach
        self._on_detach = on_detach
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
        quest_id: str | None = None
        buf = BufferedByteReceiveStream(stream)
        try:
            hello = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
            quest_id, _title, autosubscribe = parse_hello(hello)
            self._conns[quest_id] = stream
            if self._on_attach is not None:
                self._on_attach(quest_id, autosubscribe)
            while True:
                line = (await buf.receive_until(b"\n", _MAX_FRAME)).decode("utf-8")
                with contextlib.suppress(Exception):
                    self._on_event(quest_id, line)
        except (anyio.IncompleteRead, anyio.EndOfStream,
                anyio.ClosedResourceError, anyio.DelimiterNotFound,
                anyio.BrokenResourceError,
                json.JSONDecodeError, KeyError, ValueError):
            pass
        finally:
            if quest_id is not None and self._conns.get(quest_id) is stream:
                self._conns.pop(quest_id)
                if self._on_detach is not None:
                    self._on_detach(quest_id)
            await stream.aclose()

    async def close(self) -> None:
        if self._listener is not None:
            await self._listener.aclose()
            self._listener = None
