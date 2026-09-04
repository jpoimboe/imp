"""A bare frame source: raw bytes on a unix socket, including frames no daemon emits."""
from __future__ import annotations

import anyio
from anyio.abc import SocketStream, TaskGroup


async def serve_raw_frames(sock: str, payload: bytes, tg: TaskGroup) -> None:
    """Send `payload` verbatim to the first client, then close; bound before returning."""
    async def _handle(stream: SocketStream) -> None:
        async with stream:
            await stream.send(payload)

    listener = await anyio.create_unix_listener(sock)

    async def _serve() -> None:
        # anyio's serve() does not close the listener on cancellation; own it here.
        async with listener:
            await listener.serve(_handle)

    tg.start_soon(_serve)
