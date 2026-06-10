# tests/test_bg_kill.py
from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.tools import get_registry
from tests.test_bg_spawn import _ctx, _spawn


async def _kill(ctx, id: str) -> dict:
    raw = await get_registry()["bash_kill"].async_invoke(ctx, {"id": id})
    return json.loads(raw)


async def _read(ctx, id: str) -> dict:
    raw = await get_registry()["bash_output"].async_invoke(ctx, {"id": id})
    return json.loads(raw)


async def _await_group_dead(pid: int) -> None:
    """Retry briefly: a killed grandchild is reparented to init and reaped
    asynchronously, and a not-yet-reaped zombie still answers killpg(pgid, 0)."""
    with anyio.fail_after(5):
        while True:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                return
            await anyio.sleep(0.05)


def test_bash_kill_terminates_group_including_children(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "sleep 60 & sleep 60")
        r = await _kill(ctx, out["id"])
        assert r["status"] == "killed"
        assert r["kill_reason"] == "agent"
        assert isinstance(r["returncode"], int) and r["returncode"] < 0
        await _await_group_dead(out["pid"])
    anyio.run(main)


def test_bash_kill_snapshot_advances_cursors(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "echo hello; sleep 60")
        await anyio.sleep(1.0)               # let 'hello' land in the file
        r = await _kill(ctx, out["id"])
        assert "hello" in r["stdout"]
        r2 = await _read(ctx, out["id"])
        assert r2["stdout"] == ""            # no duplicate bytes
    anyio.run(main)


def test_bash_kill_idempotent_on_exited(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "exit 3")
        bp = ctx.background_processes[out["id"]]
        with anyio.fail_after(10):
            await bp.proc.wait()
        r = await _kill(ctx, out["id"])
        # exited entry keeps its terminal state — NOT relabelled killed/agent
        assert r["status"] == "exited"
        assert r["returncode"] == 3
        assert "kill_reason" not in r
        r2 = await _kill(ctx, out["id"])     # and idempotent
        assert r2["status"] == "exited"
    anyio.run(main)


def test_bash_kill_flips_before_signaling(tmp_path: Path) -> None:
    """Corollary pin: status/kill_reason are set BEFORE the (awaiting) grace
    escalation — a sweep landing mid-grace must see the terminal label.
    Costs ~4 s wall time (the TERM-trapping child rides out the full
    PROCESS_KILL_GRACE_S) — budgeted; do NOT shrink the grace constant."""
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "trap '' TERM; echo ready; sleep 60")   # survives SIGTERM
        bp = ctx.background_processes[out["id"]]

        # Don't signal until the trap is installed: SIGTERM racing the trap
        # builtin kills the group instantly — _kill returns in ~0ms, the
        # 0.5 s observer samples after completion, and the mid-grace pin
        # is hollow (a flip-after-signal regression would pass).
        seen_out = ""
        with anyio.fail_after(10):
            while "ready" not in seen_out:
                seen_out += (await _read(ctx, out["id"]))["stdout"]
                await anyio.sleep(0.05)

        seen: list[tuple[str, str | None]] = []

        async def _observe() -> None:
            await anyio.sleep(0.5)           # mid-grace (grace is 4 s)
            seen.append((bp.status, bp.kill_reason))

        async with anyio.create_task_group() as tg:
            tg.start_soon(_observe)
            await _kill(ctx, out["id"])
        assert seen == [("killed", "agent")]
    anyio.run(main)


def test_bash_kill_unknown_id(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        with pytest.raises(ValueError):
            await _kill(ctx, "bg9")
    anyio.run(main)
