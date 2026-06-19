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
