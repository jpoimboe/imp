"""Tests for the codex SDK subprocess spawn patch (read-limit + session isolation).

Covers:
- Behavioral regression: a line > 64 KiB raises ValueError at asyncio's default limit.
- The fix value (CODEX_STREAM_LIMIT) reads that same line cleanly.
- _SubprocessPatchingAsyncio proxy: injects default limit AND start_new_session,
  respects explicit values for each, forwards other attrs.
- install_codex_subprocess_patches: idempotent, replaces the SDK module ref, forwards real attrs.
- Real grouping: a child spawned through the proxy lands in its own process group.
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from imp.providers.codex_subprocess_patch import (
    CODEX_STREAM_LIMIT,
    _SubprocessPatchingAsyncio,
    install_codex_subprocess_patches,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

async def _read_one_line(limit: int) -> bytes:
    """Spawn a subprocess that writes a 200 KiB line to stdout, read it at the given limit.

    The big line is passed via a temp file (not as a CLI arg) to avoid the OS
    ARG_MAX limit on long command-line arguments.
    """
    big = "x" * (200 * 1024)  # 200 KiB > asyncio's 64 KiB default
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        f.write(big)
        tmp_path = f.name
    code = (
        f"import sys; "
        f"data = open({tmp_path!r}).read(); "
        f"sys.stdout.write(data + '\\n'); "
        f"sys.stdout.flush()"
    )
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", code,
        stdout=asyncio.subprocess.PIPE, limit=limit)
    try:
        return await proc.stdout.readline()  # type: ignore[union-attr]
    finally:
        await proc.wait()
        Path(tmp_path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# Behavioral: reproduce the bug and confirm the fix
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_default_limit_raises_on_oversized_line() -> None:
    """Reproduce: asyncio's 64 KiB default limit raises ValueError on a 200 KiB line."""
    with pytest.raises(ValueError):
        await _read_one_line(64 * 1024)


@pytest.mark.anyio
async def test_codex_limit_reads_oversized_line() -> None:
    """Fix: CODEX_STREAM_LIMIT is large enough to read a 200 KiB line cleanly."""
    line = await _read_one_line(CODEX_STREAM_LIMIT)
    assert len(line) >= 200 * 1024


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

def test_install_is_idempotent_and_forwards() -> None:
    """install_codex_subprocess_patches() is idempotent; the proxy forwards real asyncio attrs."""
    from openai_codex_sdk import exec as codex_exec  # type: ignore[import-untyped]

    original = codex_exec.asyncio
    try:
        install_codex_subprocess_patches()
        install_codex_subprocess_patches()  # second call must be a no-op
        assert isinstance(codex_exec.asyncio, _SubprocessPatchingAsyncio)
        assert codex_exec.asyncio.FIRST_COMPLETED is asyncio.FIRST_COMPLETED
        assert codex_exec.asyncio.create_subprocess_exec is not None
    finally:
        codex_exec.asyncio = original  # restore so the patch doesn't leak


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
