"""Patch the codex SDK's subprocess spawn: raise its stdout read limit and
isolate it into its own session.

`openai_codex_sdk/exec.py` reads the `codex exec` subprocess via
`proc.stdout.readline()` on a stream created by `asyncio.create_subprocess_exec`
WITHOUT a `limit=` (so it uses asyncio's 64 KiB default). A single codex JSONL
event line over 64 KiB then makes `StreamReader.readline()` raise
`ValueError("Separator is found, but chunk is longer than limit")`. The same
spawn also places the child in imp's own process group, so a terminal Ctrl-C
(a process-group SIGINT) reaches the codex CLI directly and kills it ungracefully
(rc 1) instead of letting imp's cooperative pause run.

Both are fixed by proxying the `asyncio` reference the SDK's exec module uses and
injecting defaults into `create_subprocess_exec`: a large `limit` and
`start_new_session=True` (the child becomes its own session leader, pgid == pid,
outside imp's group, so a group Ctrl-C reaches imp alone).
"""
from __future__ import annotations

from typing import Any

CODEX_STREAM_LIMIT = 64 * 1024 * 1024  # 64 MiB: bounded ceiling for one JSONL line


class _SubprocessPatchingAsyncio:
    """Proxy over the real `asyncio` module that injects a large default
    subprocess read limit and session isolation. Every attribute except
    `create_subprocess_exec` forwards unchanged to the real module."""

    def __init__(self, real: Any, limit: int) -> None:
        self._real = real
        self._limit = limit

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def create_subprocess_exec(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("limit", self._limit)
        kwargs.setdefault("start_new_session", True)
        return await self._real.create_subprocess_exec(*args, **kwargs)


def install_codex_subprocess_patches() -> None:
    """Idempotently patch the codex SDK's exec module so its
    `create_subprocess_exec` uses CODEX_STREAM_LIMIT and start_new_session=True.
    Call before driving a real codex thread. No-op if already installed."""
    import openai_codex_sdk.exec as codex_exec

    if isinstance(codex_exec.asyncio, _SubprocessPatchingAsyncio):  # type: ignore[attr-defined]
        return
    codex_exec.asyncio = _SubprocessPatchingAsyncio(  # type: ignore[attr-defined, assignment]  # proxy replaces module ref
        codex_exec.asyncio, CODEX_STREAM_LIMIT)  # type: ignore[attr-defined]
