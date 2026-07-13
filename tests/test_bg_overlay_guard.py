# tests/test_bg_overlay_guard.py
from __future__ import annotations

import json
from pathlib import Path

import anyio

import imp.agents.tools.bash  # noqa: F401
from imp.agents.tools import get_registry
from imp.engine.sandbox import (
    OVERLAY_SETUP_FAILED_CODE,
    OVERLAY_SETUP_FAILED_MARKER,
)
from tests.test_bg_spawn import _cleanup, _ctx, _spawn
from tests.test_bg_sweep import _RecLog

# Mimics build_sandbox_argv's fail-loud wrapper WITHOUT needing a real overlay:
# print the sentinel to stderr and exit the reserved code, never running a real
# command. This is exactly what the bg tool must classify as a setup failure.
_FAIL_CMD = (f"printf '%s\\n' {OVERLAY_SETUP_FAILED_MARKER} >&2; "
             f"exit {OVERLAY_SETUP_FAILED_CODE}")


async def _wait_exit(ctx, bg_id: str) -> None:
    bp = ctx.background_processes[bg_id]
    with anyio.fail_after(10):
        while bp.proc.returncode is None:
            await anyio.sleep(0.02)


def test_background_overlay_setup_failure_detected_and_reported(tmp_path: Path) -> None:
    async def main() -> None:
        from imp.agents.tools.bg import _sweep_bg
        ctx = _ctx(tmp_path)
        log = _RecLog()
        ctx.event_log = log
        out = await _spawn(ctx, _FAIL_CMD)
        try:
            await _wait_exit(ctx, out["id"])
            await _sweep_bg(ctx)
            bp = ctx.background_processes[out["id"]]
            assert bp.overlay_setup_failed is True
            # emitted exactly once with the distinct reason
            [w] = log.warnings()
            assert w["reason"] == "overlay-setup-failed"
            assert w["id"] == out["id"]
            # a later sweep must NOT re-emit
            await _sweep_bg(ctx)
            assert len(log.warnings()) == 1
            # the live snapshot surfaces the distinct sandbox-setup failure
            r = json.loads(await get_registry()["bash_output"].async_invoke(
                ctx, {"id": out["id"]}))
            assert r["status"] == "sandbox_setup_failed"
            assert "did NOT run" in r["error"]
            assert r["returncode"] == OVERLAY_SETUP_FAILED_CODE
        finally:
            await _cleanup(ctx)
    anyio.run(main)


def test_snapshot_backstop_sets_flag_without_prior_sweep(tmp_path: Path) -> None:
    # The _snapshot backstop must classify a setup failure even when NO sweep
    # ran first: call _snapshot directly on an exited proc whose flag is still
    # False. If this path is deleted, this test goes red.
    async def main() -> None:
        from imp.agents.tools.bg import _reap, _snapshot
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, _FAIL_CMD)
        try:
            await _wait_exit(ctx, out["id"])
            bp = ctx.background_processes[out["id"]]
            # Reap so returncode/status settle, but do NOT sweep (no detector run).
            _reap(bp)
            assert bp.overlay_setup_failed is False   # nothing has set it yet
            r = json.loads(_snapshot(ctx, bp))
            # The backstop alone set the flag and overrode the status.
            assert bp.overlay_setup_failed is True
            assert r["status"] == "sandbox_setup_failed"
            assert "did NOT run" in r["error"]
        finally:
            await _cleanup(ctx)
    anyio.run(main)
