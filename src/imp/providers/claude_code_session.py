"""Spawn the claude_code CLI in its own session so a terminal Ctrl-C reaches imp alone.

`claude_agent_sdk/_internal/transport/subprocess_cli.py` spawns the `claude` CLI
via `anyio.open_process(...)` WITHOUT `start_new_session`,
so the child joins imp's process group. A terminal Ctrl-C (a process-group SIGINT)
then reaches the CLI directly and kills it ungracefully (rc 1) instead of letting
imp's cooperative pause run.

Fix: proxy the module-local `anyio` reference that subprocess_cli uses and inject
`start_new_session=True` into `open_process`, making the child its own session
leader (pgid == pid, outside imp's group). Only that module's `open_process`
calls are affected; imp's own `anyio.open_process` calls are untouched.
"""
from __future__ import annotations

from typing import Any


class _SessionIsolatingAnyio:
    """Proxy over the real `anyio` module that injects session isolation into
    `open_process`. Every attribute except `open_process` forwards unchanged."""

    def __init__(self, real: Any) -> None:
        self._real = real

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def open_process(self, *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault("start_new_session", True)
        return await self._real.open_process(*args, **kwargs)


def install_claude_session_isolation() -> None:
    """Idempotently patch the claude SDK's subprocess transport so its
    `anyio.open_process` spawns the CLI with start_new_session=True. Call before
    constructing a real ClaudeSDKClient. No-op if already installed or if the SDK
    isn't importable."""
    try:
        import claude_agent_sdk._internal.transport.subprocess_cli as subprocess_cli
    except ImportError:
        return

    if isinstance(subprocess_cli.anyio, _SessionIsolatingAnyio):  # type: ignore[attr-defined]
        return
    subprocess_cli.anyio = _SessionIsolatingAnyio(subprocess_cli.anyio)  # type: ignore[attr-defined, assignment]  # proxy replaces module ref
