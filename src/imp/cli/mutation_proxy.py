# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
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

_MUTATION_OPS = frozenset({"resume", "new", "edit", "abandon"})


def run_mutation(
    control_root: Path, op: str, args: dict[str, Any],
    parent_id: str | None = None,
) -> dict[str, Any]:
    if op == "resume":
        from imp.remote.daemon import spawn
        message = spawn.resume_quest(control_root, args["token"])
        return {"returncode": 0, "message": message}
    if op == "edit":
        from imp.cli.main import _match_quest_ids, apply_edit
        token = args["token"]
        matches = _match_quest_ids(control_root, token)
        if len(matches) != 1:
            message = (f"imp: no quest matches {token!r}" if not matches
                       else f"imp: {token!r} is ambiguous: {', '.join(matches)}")
            return {"returncode": 2, "message": message}
        rc, msg = apply_edit(
            control_root, matches[0], title=args["title"], backlog=args["backlog"],
            active=args["active"], depends_on=args["depends_on"],
            clear_deps=args["clear_deps"], append_intent=args["append_intent"],
            base=args["base"])
        return {"returncode": rc, "message": msg or ""}
    if op == "new":
        from imp.cli.main import apply_new
        rc, message = apply_new(
            control_root, opening=args["opening"], title=args["title"],
            workflow=args["workflow"], base=args["base"], idea=args["idea"],
            depends_on=args["depends_on"], var_flags=args["var_flags"],
            background=args.get("background", False), parent=parent_id,
            provider=args.get("provider"))
        return {"returncode": rc, "message": message}
    if op == "abandon":
        from imp.cli.main import _abandon, _match_quest_ids
        token = args["token"]
        matches = _match_quest_ids(control_root, token)
        if len(matches) != 1:
            message = (f"imp: no quest matches {token!r}" if not matches
                       else f"imp: {token!r} is ambiguous: {', '.join(matches)}")
            return {"returncode": 2, "message": message}
        rc, msg = _abandon(control_root, matches[0])
        return {"returncode": rc, "message": msg}
    raise ValueError(f"unknown mutation op {op!r}")


def build_session_handler(
    control_root: Path, *, parent_id: str | None = None,
) -> Any:
    """Daemon on_query: mutation ops -> run_mutation, else -> run_query; both off-loop."""
    from imp.cli import store_proxy
    from imp.cli.store import LocalStore

    store = LocalStore(control_root)

    async def _on_query(op: str, args: dict[str, Any]) -> Any:
        if op in _MUTATION_OPS:
            return await anyio.to_thread.run_sync(
                run_mutation, control_root, op, args, parent_id)
        return await anyio.to_thread.run_sync(store_proxy.run_query, store, op, args)

    return _on_query


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
