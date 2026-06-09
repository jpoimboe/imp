# tests/test_bg_wait.py
from __future__ import annotations

import json
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.tools import get_registry
from imp.agents.tools.limits import ToolAborted
from tests.test_bg_spawn import _cleanup, _ctx, _spawn


async def _read(ctx, id: str, **kw) -> dict:
    raw = await get_registry()["bash_output"].async_invoke(ctx, {"id": id, **kw})
    return json.loads(raw)


def test_wait_returns_on_exit(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "sleep 0.3; echo hi; exit 5")
        r = await _read(ctx, out["id"], wait=True)
        assert r["wait_ended"] == "exit"
        assert r["status"] == "exited"
        assert r["returncode"] == 5
        assert r["stdout"] == "hi\n"
    anyio.run(main)


def test_wait_on_already_exited_returns_immediately(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "true")
        bp = ctx.background_processes[out["id"]]
        with anyio.fail_after(10):
            await bp.proc.wait()
        t0 = anyio.current_time()
        r = await _read(ctx, out["id"], wait=True)
        assert r["wait_ended"] == "exit"
        assert anyio.current_time() - t0 < 2.0
    anyio.run(main)


def test_wait_caller_timeout_is_normal_result(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "sleep 60")
        r = await _read(ctx, out["id"], wait=True, timeout=0.3)
        assert r["wait_ended"] == "timeout"
        assert r["status"] == "running"
        await _cleanup(ctx)
    anyio.run(main)


def test_wait_budget_is_normal_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        from imp.agents.tools import limits
        monkeypatch.setattr(limits, "BG_WAIT_MAX_S", 0.3)
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "sleep 60")
        r = await _read(ctx, out["id"], wait=True, timeout=3600)
        assert r["wait_ended"] == "budget"          # budget clips the timeout
        assert r["status"] == "running"
        await _cleanup(ctx)
    anyio.run(main)


def test_cap_kill_during_wait_ends_wait_as_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """wait_ended 'exit' is pinned to mean 'reached a TERMINAL status' —
    including a sweep cap-kill during the agent's own wait."""
    async def main() -> None:
        from imp.agents.tools import limits
        monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
        ctx = _ctx(tmp_path)
        out = await _spawn(
            ctx, "while true; do echo xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; done")
        r = await _read(ctx, out["id"], wait=True, timeout=30)
        assert r["wait_ended"] == "exit"
        assert r["status"] == "killed"
        assert r["kill_reason"] == "output-cap"
    anyio.run(main)


def test_pause_preempts_wait_as_tool_aborted(tmp_path: Path) -> None:
    """Pinned surface: checkpoint runs BEFORE the terminal-status return, so a
    pause deterministically raises ToolAborted('interrupted') -> is_error."""
    async def main() -> None:
        class _Coord:
            should_pause = False
        ctx = _ctx(tmp_path)
        ctx.coordinator = _Coord()
        out = await _spawn(ctx, "sleep 60")

        async def _pause_soon() -> None:
            await anyio.sleep(0.3)
            _Coord.should_pause = True

        # anyio wraps task-group body exceptions in ExceptionGroup; unwrap to
        # verify the underlying ToolAborted with reason == "interrupted".
        with pytest.raises(BaseExceptionGroup) as exc_info:
            async with anyio.create_task_group() as tg:
                tg.start_soon(_pause_soon)
                await _read(ctx, out["id"], wait=True)
        aborted = [e for e in exc_info.value.exceptions
                   if isinstance(e, ToolAborted)]
        assert aborted and aborted[0].reason == "interrupted"
        await _cleanup(ctx)
    anyio.run(main)


def test_wait_does_not_busy_spin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real inter-poll sleep, not just the checkpoint's sleep(0): a 0.5 s
    wait must take ~10 ticks at 0.05 s, not thousands."""
    async def main() -> None:
        import imp.agents.tools.bg as bg_mod
        calls = {"n": 0}
        real = bg_mod._sweep_bg

        async def counting(ctx):
            calls["n"] += 1
            await real(ctx)

        monkeypatch.setattr(bg_mod, "_sweep_bg", counting)
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "sleep 0.5")
        await _read(ctx, out["id"], wait=True)
        assert calls["n"] < 100
    anyio.run(main)


def test_timeout_argument_validation(tmp_path: Path) -> None:
    async def main() -> None:
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "sleep 60")
        with pytest.raises(ValueError):
            await _read(ctx, out["id"], timeout=5)             # without wait
        with pytest.raises(ValueError):
            await _read(ctx, out["id"], wait=True, timeout=0)  # <= 0
        with pytest.raises(ValueError):
            await _read(ctx, out["id"], wait=True, timeout=-1)
        await _cleanup(ctx)
    anyio.run(main)
