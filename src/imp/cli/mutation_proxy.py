"""Client + daemon transport for mutating `imp` CLI commands inside the sandbox.

Mirrors `store_proxy`: the in-sandbox command ships `(op, args)` to the quest
daemon over the session socket; the daemon runs the real mutation host-side and
returns a `{"returncode": int, "message": str}` frame the sandbox replays. Read
ops still flow through `run_query`; `build_session_handler` routes between them."""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import anyio

from imp.remote.session.protocol import encode_query, parse_result

_MUTATION_OPS = frozenset({"resume", "new", "edit"})


def run_mutation(control_root: Path, op: str, args: dict[str, Any]) -> dict[str, Any]:
    if op == "resume":
        from imp.remote.daemon import spawn
        message = spawn.resume_quest(control_root, args["token"])
        return {"returncode": 0, "message": message}
    raise ValueError(f"unknown mutation op {op!r}")


async def _roundtrip(sock: str, op: str, args: dict[str, Any]) -> Any:
    stream = await anyio.connect_unix(sock)
    try:
        await stream.send((encode_query(op, args) + "\n").encode("utf-8"))
        chunks: list[bytes] = []
        try:
            while True:
                chunks.append(await stream.receive())
        except (anyio.EndOfStream, anyio.ClosedResourceError,
                anyio.BrokenResourceError):
            pass
        return parse_result(b"".join(chunks).decode("utf-8").rstrip("\n"))
    finally:
        await stream.aclose()


async def proxy_mutation_async(op: str, args: dict[str, Any]) -> Any:
    return await _roundtrip(os.environ["IMP_SESSION_SOCKET"], op, args)


def proxy_mutation(op: str, args: dict[str, Any]) -> Any:
    return anyio.run(proxy_mutation_async, op, args)
