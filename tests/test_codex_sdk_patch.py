"""Tests for imp's patches against the pinned codex SDK (subprocess spawn + item models).

Covers:
- Behavioral regression: a line > 64 KiB raises ValueError at asyncio's default limit.
- The fix value (CODEX_STREAM_LIMIT) reads that same line cleanly.
- Cleanup guard: the over-limit read abandons stdout, so the helper kills the child
  rather than waiting on it.
- _SubprocessPatchingAsyncio proxy: injects default limit AND start_new_session,
  respects explicit values for each, forwards other attrs.
- install_codex_sdk_patches: idempotent, replaces the SDK module ref, forwards real attrs.
- Widened item models: the three real codex wire payloads parse; each model is
  registered exactly once.
- Real grouping: a child spawned through the proxy lands in its own process group.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from imp.providers.codex_sdk_patch import (
    CODEX_STREAM_LIMIT,
    _CommandExecutionItem,
    _FileChangeItem,
    _McpToolCallItem,
    _SubprocessPatchingAsyncio,
    install_codex_sdk_patches,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

_BIG_LINE = 200 * 1024  # 200 KiB > asyncio's 64 KiB default

_FAST_WRITER = (
    "import sys; "
    f"sys.stdout.write('x' * {_BIG_LINE} + '\\n'); "
    "sys.stdout.flush()"
)

# The same line in small, spaced-out chunks: the feed granularity a loaded machine
# produces, which pauses the reader early enough to block the child mid-write.
_SLOW_WRITER = (
    "import sys, time\n"
    f"data = 'x' * {_BIG_LINE} + '\\n'\n"
    "for i in range(0, len(data), 4096):\n"
    "    sys.stdout.write(data[i:i + 4096]); sys.stdout.flush(); time.sleep(0.0005)\n"
)


async def _read_one_line(limit: int, writer: str = _FAST_WRITER) -> bytes:
    """Spawn a subprocess that writes a 200 KiB line to stdout, read one line at `limit`."""
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", writer,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=limit)
    try:
        line = await proc.stdout.readline()  # type: ignore[union-attr]
    finally:
        # Over the limit readline abandons stdout, leaving the child blocked
        # mid-write: a graceful wait() would never return.
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        stderr = (await proc.stderr.read()).decode("utf-8", "replace")  # type: ignore[union-attr]
        await proc.wait()
    if len(line) < _BIG_LINE:
        raise AssertionError(
            f"child delivered {len(line)} bytes, expected >= {_BIG_LINE} "
            f"(exit {proc.returncode}); child stderr: {stderr!r}")
    return line


# ---------------------------------------------------------------------------
# Behavioral: reproduce the bug and confirm the fix
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_default_limit_raises_on_oversized_line() -> None:
    """Reproduce: asyncio's 64 KiB default limit raises ValueError on a 200 KiB line."""
    with pytest.raises(ValueError):
        await _read_one_line(64 * 1024)


@pytest.mark.anyio
async def test_default_limit_raises_when_the_child_writes_slowly() -> None:
    """The over-limit read abandons stdout, so cleanup must not wait for the child to
    finish writing into a pipe nobody drains."""
    with pytest.raises(ValueError):
        await _read_one_line(64 * 1024, _SLOW_WRITER)


@pytest.mark.anyio
async def test_a_broken_child_reports_its_exit_and_stderr() -> None:
    """A child that dies without writing the line self-reports: byte count, exit status, stderr."""
    broken = "import sys; sys.stderr.write('boom'); sys.stderr.flush(); sys.exit(3)"
    with pytest.raises(AssertionError) as excinfo:
        await _read_one_line(64 * 1024, broken)
    message = str(excinfo.value)
    assert "child delivered 0 bytes" in message
    assert "expected >= 204800" in message
    assert "exit 3" in message
    assert "boom" in message


@pytest.mark.anyio
@pytest.mark.parametrize("writer", [_FAST_WRITER, _SLOW_WRITER], ids=["fast", "slow"])
async def test_codex_limit_reads_oversized_line(writer: str) -> None:
    """Fix: CODEX_STREAM_LIMIT is large enough to read a 200 KiB line cleanly."""
    line = await _read_one_line(CODEX_STREAM_LIMIT, writer)
    assert len(line) == _BIG_LINE + 1


# ---------------------------------------------------------------------------
# Proxy unit test
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_proxy_injects_limit_and_session_isolation() -> None:
    """_SubprocessPatchingAsyncio injects a default limit AND start_new_session,
    respects explicit values for each, and forwards other attributes unchanged."""
    calls: list[dict[str, Any]] = []

    class _FakeAsyncio:
        SENTINEL = 7

        async def create_subprocess_exec(self, *a: Any, **k: Any) -> str:
            calls.append(k)
            return "proc"

    proxy = _SubprocessPatchingAsyncio(_FakeAsyncio(), 123)

    await proxy.create_subprocess_exec("prog")
    assert calls[-1]["limit"] == 123, "default limit should be injected"
    assert calls[-1]["start_new_session"] is True, "session isolation should be injected"

    await proxy.create_subprocess_exec("prog", limit=999, start_new_session=False)
    assert calls[-1]["limit"] == 999, "explicit limit must win over default"
    assert calls[-1]["start_new_session"] is False, "explicit session flag must win"

    assert proxy.SENTINEL == 7, "non-overridden attributes should forward unchanged"


# ---------------------------------------------------------------------------
# Installer: idempotency + real attr forwarding
# ---------------------------------------------------------------------------

def test_install_is_idempotent_and_forwards(sdk_patch_sandbox: dict[str, Any]) -> None:
    """install_codex_sdk_patches() is idempotent; the proxy forwards real asyncio attrs."""
    from openai_codex_sdk import exec as codex_exec  # type: ignore[import-untyped]

    install_codex_sdk_patches()
    install_codex_sdk_patches()  # second call must be a no-op
    assert isinstance(codex_exec.asyncio, _SubprocessPatchingAsyncio)
    assert codex_exec.asyncio.FIRST_COMPLETED is asyncio.FIRST_COMPLETED
    assert codex_exec.asyncio.create_subprocess_exec is not None


# ---------------------------------------------------------------------------
# Real grouping integration test (acceptance #5)
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_proxy_spawns_child_in_its_own_process_group() -> None:
    """Through the real proxy, create_subprocess_exec spawns a session-isolated
    child: its process-group id differs from imp's own."""
    proxy = _SubprocessPatchingAsyncio(asyncio, CODEX_STREAM_LIMIT)
    proc = await proxy.create_subprocess_exec(
        sys.executable, "-c", "import time; time.sleep(30)",
        stdout=asyncio.subprocess.PIPE)
    try:
        assert os.getpgid(proc.pid) != os.getpgid(0)
    finally:
        proc.kill()
        await proc.wait()


# ---------------------------------------------------------------------------
# Tee: stderr reads feed the active sink while still returning all bytes
# ---------------------------------------------------------------------------

from imp.providers.stderr_sink import StderrSink, current_stderr_sink  # noqa: E402


class _FakeStream:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    async def read(self, _n: int = -1) -> bytes:
        return self._chunks.pop(0) if self._chunks else b""


class _FakeProc:
    def __init__(self) -> None:
        self.stderr = _FakeStream([b"warn: line one\n", b"err: line two\n"])


class _FakeAsyncioWithStderr:
    """Stand-in for the real asyncio module the proxy wraps."""

    async def create_subprocess_exec(self, *_a: Any, **_kw: Any) -> _FakeProc:
        return _FakeProc()

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - unused passthrough
        return getattr(asyncio, name)


@pytest.mark.anyio
async def test_tee_feeds_active_sink_while_draining(tmp_path: Path) -> None:
    sink = StderrSink(tmp_path / "stderr.log")
    token = current_stderr_sink.set(sink)
    try:
        proxy = _SubprocessPatchingAsyncio(_FakeAsyncioWithStderr(), CODEX_STREAM_LIMIT)
        proc = await proxy.create_subprocess_exec("codex")
        # Drain exactly as the SDK's _read_all does.
        out = b""
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            out += chunk
    finally:
        current_stderr_sink.reset(token)
    sink.close()

    # The real reader still yielded every byte (no double-drain / starvation)...
    assert out == b"warn: line one\nerr: line two\n"
    # ...and the sink saw the same bytes.
    assert sink.tail() == "warn: line one\nerr: line two\n"


@pytest.mark.anyio
async def test_tee_noop_when_no_active_sink() -> None:
    proxy = _SubprocessPatchingAsyncio(_FakeAsyncioWithStderr(), CODEX_STREAM_LIMIT)
    proc = await proxy.create_subprocess_exec("codex")
    # No sink set → stderr is the untouched real stream.
    assert await proc.stderr.read(4096) == b"warn: line one\n"


class _FakeProcSplitChar:
    """stderr where a multi-byte UTF-8 char ('é' = b'\\xc3\\xa9') is split across
    two reads, to exercise the tee's decode across a read boundary."""

    def __init__(self) -> None:
        self.stderr = _FakeStream([b"caf\xc3", b"\xa9\n"])


class _FakeAsyncioSplitChar:
    async def create_subprocess_exec(self, *_a: Any, **_kw: Any) -> _FakeProcSplitChar:
        return _FakeProcSplitChar()

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - unused passthrough
        return getattr(asyncio, name)


@pytest.mark.anyio
async def test_tee_decodes_multibyte_char_split_across_reads(tmp_path: Path) -> None:
    sink = StderrSink(tmp_path / "stderr.log")
    token = current_stderr_sink.set(sink)
    try:
        proxy = _SubprocessPatchingAsyncio(_FakeAsyncioSplitChar(), CODEX_STREAM_LIMIT)
        proc = await proxy.create_subprocess_exec("codex")
        out = b""
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            out += chunk
    finally:
        current_stderr_sink.reset(token)
    sink.close()

    # Every raw byte still reaches the drainer unchanged...
    assert out == b"caf\xc3\xa9\n"
    # ...and the sink decodes the split 'é' correctly (no U+FFFD replacement).
    assert sink.tail() == "café\n"


# ---------------------------------------------------------------------------
# Item models: the pinned SDK's models vs. the codex CLI's real wire contract
# ---------------------------------------------------------------------------

# Real codex 0.153 wire lines (codex-rs exec/src/exec_events.rs at rust-v0.153.2).
_WIRE_FILE_CHANGE_STARTED = (
    '{"type":"item.started","item":{"id":"i1","type":"file_change",'
    '"changes":[{"path":"a","kind":"add"}],"status":"in_progress"}}'
)
_WIRE_COMMAND_DECLINED = (
    '{"type":"item.completed","item":{"id":"i2","type":"command_execution",'
    '"command":"ls","aggregated_output":"","exit_code":null,"status":"declined"}}'
)
_WIRE_MCP_STRING_CONTENT = (
    '{"type":"item.completed","item":{"id":"i3","type":"mcp_tool_call",'
    '"server":"imp","tool":"bash","arguments":{},'
    '"result":{"content":["plain string"],"structured_content":null},'
    '"status":"completed"}}'
)


@pytest.fixture
def sdk_patch_sandbox() -> Iterator[dict[str, Any]]:
    """Install the patches inside one test only: snapshot the SDK's item-model
    registry and its exec-module asyncio ref and restore both afterwards. Yields
    the pre-install registry snapshot."""
    from openai_codex_sdk import exec as codex_exec
    from openai_codex_sdk import parsing

    saved_models = dict(parsing._ITEM_MODELS)
    saved_asyncio = codex_exec.asyncio
    try:
        yield saved_models
    finally:
        parsing._ITEM_MODELS.clear()
        parsing._ITEM_MODELS.update(saved_models)
        codex_exec.asyncio = saved_asyncio


def test_patched_parser_accepts_the_real_wire_lines(
    sdk_patch_sandbox: dict[str, Any],
) -> None:
    """The three payloads codex actually emits parse, with their real values."""
    from openai_codex_sdk.parsing import parse_thread_event_line

    from imp.providers.codex_runner import _serialize_mcp_result

    install_codex_sdk_patches()

    started = parse_thread_event_line(_WIRE_FILE_CHANGE_STARTED)
    assert started.item.status == "in_progress"
    assert [c.path for c in started.item.changes] == ["a"]

    declined = parse_thread_event_line(_WIRE_COMMAND_DECLINED)
    assert declined.item.status == "declined"
    assert declined.item.command == "ls"

    mcp = parse_thread_event_line(_WIRE_MCP_STRING_CONTENT)
    assert mcp.item.result.content == ["plain string"]
    assert _serialize_mcp_result(mcp.item.result) == "plain string"


def test_install_registers_each_item_model_exactly_once(
    sdk_patch_sandbox: dict[str, Any],
) -> None:
    """Two calls leave one registration per key and one subprocess proxy."""
    from openai_codex_sdk import exec as codex_exec
    from openai_codex_sdk import parsing

    install_codex_sdk_patches()
    install_codex_sdk_patches()

    assert set(parsing._ITEM_MODELS) == set(sdk_patch_sandbox)
    assert parsing._ITEM_MODELS["file_change"] is _FileChangeItem
    assert parsing._ITEM_MODELS["command_execution"] is _CommandExecutionItem
    assert parsing._ITEM_MODELS["mcp_tool_call"] is _McpToolCallItem
    assert isinstance(codex_exec.asyncio, _SubprocessPatchingAsyncio)
    assert not isinstance(codex_exec.asyncio._real, _SubprocessPatchingAsyncio)


def test_unpatched_sdk_models_still_reject_the_real_wire_payloads() -> None:
    """Obsolescence guard: if upstream ever fixes these models, this test fails
    and tells us imp's widened subclasses are dead code."""
    import pydantic
    from openai_codex_sdk.types import (
        CommandExecutionItem,
        FileChangeItem,
        McpToolCallResult,
    )

    with pytest.raises(pydantic.ValidationError):
        FileChangeItem.model_validate(json.loads(_WIRE_FILE_CHANGE_STARTED)["item"])
    with pytest.raises(pydantic.ValidationError):
        CommandExecutionItem.model_validate(json.loads(_WIRE_COMMAND_DECLINED)["item"])
    with pytest.raises(pydantic.ValidationError):
        McpToolCallResult.model_validate(json.loads(_WIRE_MCP_STRING_CONTENT)["item"]["result"])
