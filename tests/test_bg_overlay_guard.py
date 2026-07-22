# tests/test_bg_overlay_guard.py
from __future__ import annotations

import json
from pathlib import Path

import anyio

import imp.agents.tools.bash  # noqa: F401
from imp.agents.tools import get_registry
from imp.engine.sandbox import SANDBOX_ENTERED_MARKER
from tests.async_wait import wait_until
from tests.test_bg_spawn import _cleanup, _ctx, _spawn
from tests.test_bg_sweep import _RecLog

# A sandboxed spawn that FAILED TO ENTER: exits nonzero and never emits the entry
# sentinel. The bg tool records sandbox_active from ctx.sandbox_policy (None in a
# bare test ctx -> False), so each test forces bp.sandbox_active = True to model a
# spawn where the sandbox WAS active.
_FAIL_CMD = "exit 3"
# A healthy sandboxed command: emits the entry sentinel (as the real argv builder
# would) then real output.
_OK_CMD = f"printf '%s\\n' {SANDBOX_ENTERED_MARKER} >&2; echo real >&2; echo out"


async def _wait_exit(ctx, bg_id: str) -> None:
    bp = ctx.background_processes[bg_id]
    await wait_until(lambda: bp.proc.returncode is not None)


def _mark_sandboxed(ctx, bg_id: str) -> None:
    ctx.background_processes[bg_id].sandbox_active = True


def test_background_spawn_failure_detected_and_reported(tmp_path: Path) -> None:
    async def main() -> None:
        from imp.agents.tools.bg import _sweep_bg
        ctx = _ctx(tmp_path)
        log = _RecLog()
        ctx.event_log = log
        out = await _spawn(ctx, _FAIL_CMD)
        _mark_sandboxed(ctx, out["id"])
        try:
            await _wait_exit(ctx, out["id"])
            await _sweep_bg(ctx)
            bp = ctx.background_processes[out["id"]]
            assert bp.overlay_setup_failed is True
            [w] = log.warnings()
            assert w["reason"] == "sandbox-spawn-failed"
            assert w["id"] == out["id"]
            # a later sweep must NOT re-emit or re-read
            await _sweep_bg(ctx)
            assert len(log.warnings()) == 1
            r = json.loads(await get_registry()["bash_output"].async_invoke(
                ctx, {"id": out["id"]}))
            assert r["status"] == "sandbox_setup_failed"
            assert "did NOT run" in r["error"]
        finally:
            await _cleanup(ctx)
    anyio.run(main)


def test_background_spawn_failure_survives_teardown(tmp_path: Path) -> None:
    async def main() -> None:
        from imp.agents.tools.bg import (
            _sweep_bg,
            terminate_background_processes,
        )
        ctx = _ctx(tmp_path)
        ctx.event_log = _RecLog()
        out = await _spawn(ctx, _FAIL_CMD)
        _mark_sandboxed(ctx, out["id"])
        await _wait_exit(ctx, out["id"])
        await _sweep_bg(ctx)                          # flag set via sweep
        await terminate_background_processes(ctx)     # writes the tombstone file
        ctx2 = _ctx(tmp_path)
        r = json.loads(await get_registry()["bash_output"].async_invoke(
            ctx2, {"id": out["id"]}))
        assert r["status"] == "sandbox_setup_failed"
        assert "did NOT run" in r["error"]
    anyio.run(main)


def test_background_spawn_failure_detected_in_teardown(tmp_path: Path) -> None:
    async def main() -> None:
        from imp.agents.tools.bg import terminate_background_processes
        ctx = _ctx(tmp_path)
        ctx.event_log = _RecLog()
        out = await _spawn(ctx, _FAIL_CMD)
        _mark_sandboxed(ctx, out["id"])
        await _wait_exit(ctx, out["id"])
        await terminate_background_processes(ctx)     # NO prior sweep
        ctx2 = _ctx(tmp_path)
        r = json.loads(await get_registry()["bash_output"].async_invoke(
            ctx2, {"id": out["id"]}))
        assert r["status"] == "sandbox_setup_failed"
        assert "did NOT run" in r["error"]
    anyio.run(main)


def test_snapshot_does_not_flag_spawn_failure(tmp_path: Path) -> None:
    # Detection lives ONLY in _detect_overlay_setup_failure now; _snapshot never
    # sets the flag (a naive per-page swap would read True on later pages once the
    # marker scrolled past the cursor). If per-page detection is re-added, this
    # goes red.
    async def main() -> None:
        from imp.agents.tools.bg import _reap, _snapshot
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, _FAIL_CMD)
        _mark_sandboxed(ctx, out["id"])
        try:
            await _wait_exit(ctx, out["id"])
            bp = ctx.background_processes[out["id"]]
            _reap(bp)
            r = json.loads(_snapshot(ctx, bp))
            assert bp.overlay_setup_failed is False    # snapshot did NOT detect
            assert r["status"] == "exited"
        finally:
            await _cleanup(ctx)
    anyio.run(main)


def test_healthy_sandboxed_background_strips_marker(tmp_path: Path) -> None:
    # A healthy sandboxed command emits the sentinel as its first stderr line; the
    # agent-visible stderr must NOT carry it, and it is NOT a spawn failure.
    async def main() -> None:
        from imp.agents.tools.bg import _sweep_bg
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, _OK_CMD)
        _mark_sandboxed(ctx, out["id"])
        try:
            await _wait_exit(ctx, out["id"])
            await _sweep_bg(ctx)
            r = json.loads(await get_registry()["bash_output"].async_invoke(
                ctx, {"id": out["id"]}))
            assert r["status"] != "sandbox_setup_failed"
            assert SANDBOX_ENTERED_MARKER not in r["stderr"]
            assert "real" in r["stderr"]
        finally:
            await _cleanup(ctx)
    anyio.run(main)


def test_healthy_terminal_bg_stderr_read_at_most_once(
    tmp_path: Path, monkeypatch,
) -> None:
    # The one-shot check guard: dropping the reserved-code short-circuit must NOT
    # re-read a healthy terminal proc's whole err_path on every sweep.
    async def main() -> None:
        from imp.agents.tools.bg import _sweep_bg
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, _OK_CMD)
        _mark_sandboxed(ctx, out["id"])
        try:
            bp = ctx.background_processes[out["id"]]
            await _wait_exit(ctx, out["id"])
            calls = {"n": 0}
            real = Path.read_text

            def counting(self, *a, **k):
                if self == bp.err_path:
                    calls["n"] += 1
                return real(self, *a, **k)

            monkeypatch.setattr(Path, "read_text", counting)
            await _sweep_bg(ctx)
            await _sweep_bg(ctx)
            assert calls["n"] == 1                      # whole-file read runs once
            assert bp.overlay_setup_failed is False
            assert bp.overlay_reported is True          # guard marked it checked
        finally:
            await _cleanup(ctx)
    anyio.run(main)
