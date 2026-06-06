"""Tests for the codex SDK subprocess stdout read-limit patch.

Covers:
- Behavioral regression: a line > 64 KiB raises ValueError at asyncio's default limit.
- The fix value (CODEX_STREAM_LIMIT) reads that same line cleanly.
- _LimitRaisingAsyncio proxy: injects default limit, respects explicit limit, forwards attrs.
- install_codex_stream_limit: idempotent, replaces the SDK module ref, forwards real attrs.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from imp.providers.codex_stream_limit import (
    CODEX_STREAM_LIMIT,
    _LimitRaisingAsyncio,
    install_codex_stream_limit,
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
# Proxy unit tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_proxy_injects_default_limit_and_respects_explicit() -> None:
    """_LimitRaisingAsyncio injects default limit when absent, respects explicit limit,
    and forwards other attributes unchanged."""
    calls: list[dict[str, Any]] = []

    class _FakeAsyncio:
        SENTINEL = 7

        async def create_subprocess_exec(self, *a: Any, **k: Any) -> str:
            calls.append(k)
            return "proc"

    proxy = _LimitRaisingAsyncio(_FakeAsyncio(), 123)
    await proxy.create_subprocess_exec("prog")
    assert calls[-1]["limit"] == 123, "default limit should be injected"

    await proxy.create_subprocess_exec("prog", limit=999)
    assert calls[-1]["limit"] == 999, "explicit limit must win over default"

    assert proxy.SENTINEL == 7, "non-overridden attributes should forward unchanged"


# ---------------------------------------------------------------------------
# Installer: idempotency + real attr forwarding + isolation (restores original)
# ---------------------------------------------------------------------------

def test_install_is_idempotent_and_forwards() -> None:
    """install_codex_stream_limit() is idempotent and the proxy forwards real asyncio attrs."""
    from openai_codex_sdk import exec as codex_exec  # type: ignore[import-untyped]

    original = codex_exec.asyncio
    try:
        install_codex_stream_limit()
        install_codex_stream_limit()  # second call must be a no-op
        assert isinstance(codex_exec.asyncio, _LimitRaisingAsyncio)
        assert codex_exec.asyncio.FIRST_COMPLETED is asyncio.FIRST_COMPLETED
        assert codex_exec.asyncio.create_subprocess_exec is not None
    finally:
        codex_exec.asyncio = original  # restore so the patch doesn't leak
