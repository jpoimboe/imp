# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""imp's compensations for the pinned `openai-codex-sdk` (0.1.11).

The pin is head of an unmaintained package, so every local fix for its divergence
from the codex CLI's real behavior lives here and is installed together by
`install_codex_sdk_patches()`.

Subprocess spawn — stdout read limit and session isolation.
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

Item models — the pinned models contradict the codex CLI's real wire contract.
`openai_codex_sdk.types` declares narrower `status` literals than codex-rs emits
and models MCP result content as objects when rust sends arbitrary JSON, so a
real event line raises `pydantic.ValidationError` out of the SDK's parser. imp
registers widened subclasses into `openai_codex_sdk.parsing._ITEM_MODELS`; each
widening mirrors the exact rust variant set, so genuinely new upstream drift
still fails loudly.

Parse-failure policy — a payload that still fails to validate is split by whether
imp consumes the item type. `parse_thread_item` is wrapped: a consumed type
raises `CodexEventParseError` (a `ConfigError`, so the runner's existing
`ProviderError` catch classifies it as `config` → pause, no retry); every other
type degrades to `_UnparsedThreadItem`, which the runner logs and skips. The
degradation lives inside the parser because once an exception escapes the SDK's
async generator the event stream cannot be resumed.
"""
from __future__ import annotations

import codecs
from typing import Any, Literal

from openai_codex_sdk.parsing import parse_thread_item as _sdk_parse_thread_item
from openai_codex_sdk.types import (
    CommandExecutionItem,
    FileChangeItem,
    McpToolCallItem,
    McpToolCallResult,
    ThreadItem,
    UnknownThreadItem,
)
from pydantic import ValidationError

from imp.providers.errors import ConfigError

CODEX_STREAM_LIMIT = 64 * 1024 * 1024  # 64 MiB: bounded ceiling for one JSONL line


class _TeeStderrReader:
    """Wraps a subprocess StreamReader so every read is also fed to the active
    StderrSink. A single consumer (the SDK's _read_all) drives reads; the tee
    observes them in passing, so there is no second reader to starve the stream."""

    def __init__(self, real: Any, sink: Any) -> None:
        self._real = real
        self._sink = sink
        # Incremental so a multi-byte char split across two reads decodes
        # correctly instead of becoming U+FFFD on the boundary.
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def __getattr__(self, name: str) -> Any:
        return getattr(self._real, name)

    async def read(self, n: int = -1) -> bytes:
        chunk: bytes = await self._real.read(n)
        text = self._decoder.decode(chunk, final=not chunk)
        if text:
            self._sink.feed(text)
        return chunk


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
        proc = await self._real.create_subprocess_exec(*args, **kwargs)
        from imp.providers.stderr_sink import current_stderr_sink

        sink = current_stderr_sink.get()
        if sink is not None and getattr(proc, "stderr", None) is not None:
            proc.stderr = _TeeStderrReader(proc.stderr, sink)
        return proc


class _FileChangeItem(FileChangeItem):
    # rust PatchApplyStatus, plus the InProgress the exec mapper emits at item.started
    status: Literal["in_progress", "completed", "failed"]  # type: ignore[assignment]


class _CommandExecutionItem(CommandExecutionItem):
    # rust CommandExecutionStatus
    status: Literal["in_progress", "completed", "failed", "declined"]  # type: ignore[assignment]


class _McpToolCallResult(McpToolCallResult):
    # rust sends Vec<JsonValue>: a content entry need not be an object
    content: list[Any]


class _McpToolCallItem(McpToolCallItem):
    result: _McpToolCallResult | None = None


# The item types process_event dispatches on: their payloads carry imp's control
# flow, so a parse failure there is fatal rather than skippable.
_CONSUMED_ITEM_TYPES = frozenset(
    {"mcp_tool_call", "agent_message", "reasoning", "web_search", "error"})


class CodexEventParseError(ConfigError):
    """A codex item type imp consumes failed to validate against the pinned SDK."""


class _UnparsedThreadItem(UnknownThreadItem):
    imp_parse_error: str


def _str_or_empty(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _parse_thread_item(data: Any) -> ThreadItem:
    """Delegate to the SDK's parser, splitting a validation failure by whether imp
    consumes the item type: fatal for consumed types, degraded for the rest."""
    try:
        return _sdk_parse_thread_item(data)
    except ValidationError as exc:
        # Reached only past the SDK's own checks, so `data` is a dict here.
        item_type = _str_or_empty(data.get("type"))
        if item_type in _CONSUMED_ITEM_TYPES:
            raise CodexEventParseError(
                f"codex event parse failure: item type {item_type!r} does not match the "
                "pinned openai-codex-sdk model — update src/imp/providers/codex_sdk_patch.py",
                detail={"item_type": item_type, "validation_error": str(exc)},
            ) from exc
        return _UnparsedThreadItem(
            id=_str_or_empty(data.get("id")), type=item_type, imp_parse_error=str(exc))


def install_codex_sdk_patches() -> None:
    """Idempotently install imp's patches against the pinned codex SDK: widened
    item models and the parse-failure policy registered into its parser, and its
    exec module's `create_subprocess_exec` given CODEX_STREAM_LIMIT and
    start_new_session=True. Call before driving a real codex thread."""
    import openai_codex_sdk.exec as codex_exec
    from openai_codex_sdk import parsing

    # Ahead of the proxy guard below: an already-proxied SDK must not skip this.
    parsing._ITEM_MODELS["file_change"] = _FileChangeItem
    parsing._ITEM_MODELS["command_execution"] = _CommandExecutionItem
    parsing._ITEM_MODELS["mcp_tool_call"] = _McpToolCallItem
    # `parse_thread_event` resolves this as a module global, and the wrapper calls
    # the function object captured at import, so a repeat call cannot re-wrap.
    parsing.parse_thread_item = _parse_thread_item

    if isinstance(codex_exec.asyncio, _SubprocessPatchingAsyncio):  # type: ignore[attr-defined]
        return
    codex_exec.asyncio = _SubprocessPatchingAsyncio(  # type: ignore[attr-defined, assignment]  # proxy replaces module ref
        codex_exec.asyncio, CODEX_STREAM_LIMIT)  # type: ignore[attr-defined]
