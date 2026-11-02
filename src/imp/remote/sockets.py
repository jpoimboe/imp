# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""anyio UNIX sockets that wait via wait_readable/wait_writable, avoiding a cancel race."""

from __future__ import annotations

import os
import socket
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import anyio
import anyio.lowlevel
from anyio.abc import SocketAttribute, SocketListener, SocketStream, UNIXSocketStream


async def connect_unix(path: str | os.PathLike[str]) -> SocketStream:
    return _SafeSocketStream(await anyio.connect_unix(path))


async def create_unix_listener(path: str | os.PathLike[str]) -> SocketListener:
    return _SafeSocketListener(await anyio.create_unix_listener(path))


class _SafeSocketStream(SocketStream):
    def __init__(self, inner: SocketStream) -> None:
        self._inner = inner
        self._raw: socket.socket = inner.extra(SocketAttribute.raw_socket)
        self._closing = False
        self._receive_guard = anyio.ResourceGuard("reading from")
        self._send_guard = anyio.ResourceGuard("writing to")

    @property
    def _raw_socket(self) -> socket.socket:
        return self._raw

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return self._inner.extra_attributes

    async def receive(self, max_bytes: int = 65536) -> bytes:
        await anyio.lowlevel.checkpoint()
        with self._receive_guard:
            while True:
                try:
                    data = self._raw.recv(max_bytes)
                except BlockingIOError:
                    await self._wait(anyio.wait_readable)
                    continue
                except OSError as exc:
                    raise _os_error(self._closing) from exc
                if not data:
                    raise anyio.EndOfStream
                return data

    async def send(self, item: bytes) -> None:
        await anyio.lowlevel.checkpoint()
        with self._send_guard:
            view = memoryview(item)
            while view:
                try:
                    view = view[self._raw.send(view):]
                except BlockingIOError:
                    await self._wait(anyio.wait_writable)
                except OSError as exc:
                    raise _os_error(self._closing) from exc

    async def send_eof(self) -> None:
        self._raw.shutdown(socket.SHUT_WR)

    async def aclose(self) -> None:
        if self._closing:
            return
        self._closing = True
        # Wake parked waiters first, else they hang and a reused fd hits BusyResourceError.
        if self._raw.fileno() != -1:
            anyio.notify_closing(self._raw)
        await self._inner.aclose()

    async def _wait(self, wait: Callable[[socket.socket], Awaitable[None]]) -> None:
        if self._closing:
            raise anyio.ClosedResourceError
        await wait(self._raw)


class _SafeSocketListener(SocketListener):
    def __init__(self, inner: SocketListener) -> None:
        self._inner = inner
        self._raw: socket.socket = inner.extra(SocketAttribute.raw_socket)
        self._closing = False
        self._accept_guard = anyio.ResourceGuard("accepting connections from")

    @property
    def _raw_socket(self) -> socket.socket:
        return self._raw

    @property
    def extra_attributes(self) -> Mapping[Any, Callable[[], Any]]:
        return self._inner.extra_attributes

    async def accept(self) -> SocketStream:
        await anyio.lowlevel.checkpoint()
        with self._accept_guard:
            while True:
                try:
                    sock, _ = self._raw.accept()
                except BlockingIOError:
                    if self._closing:
                        raise anyio.ClosedResourceError from None
                    await anyio.wait_readable(self._raw)
                    continue
                except OSError as exc:
                    raise _os_error(self._closing) from exc
                sock.setblocking(False)
                return _SafeSocketStream(await UNIXSocketStream.from_socket(sock))

    async def aclose(self) -> None:
        if self._closing:
            return
        self._closing = True
        if self._raw.fileno() != -1:
            anyio.notify_closing(self._raw)
        await self._inner.aclose()


def _os_error(closing: bool) -> Exception:
    return anyio.ClosedResourceError() if closing else anyio.BrokenResourceError()
