# tests/test_bg_sweep.py
from __future__ import annotations

import json
import os
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.tools import get_registry
from tests.test_bg_spawn import _cleanup, _ctx, _spawn

FLOODER = "while true; do echo xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx; done"


class _RecLog:
    """Minimal EventLog stand-in: enough surface for the sweep (append) and
    for dispatch_one_tool_use (append_tool_call/append_tool_result)."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def append(self, *, agent_id, type, payload):
        self.events.append((type, payload))
        return {"id": "e", "type": type}

    def append_tool_call(self, *, agent_id, block):
        self.events.append(("tool_call", {"id": block.id, "name": block.name}))
        return {"id": "e", "type": "tool_call"}

    def append_tool_result(self, *, agent_id, block, citations=None):
        self.events.append(("tool_result", {"id": block.tool_use_id}))
        return {"id": "e", "type": "tool_result"}

    def warnings(self) -> list[dict]:
        return [p for t, p in self.events if t == "runner_warning"]


async def _sweep_until_killed(ctx, bg_id: str, attempts: int = 100) -> None:
    from imp.agents.tools.bg import _sweep_bg
    for _ in range(attempts):
        await _sweep_bg(ctx)
        if ctx.background_processes[bg_id].status == "killed":
            return
        await anyio.sleep(0.1)
    raise AssertionError("flooder never cap-killed")


def test_finished_unread_process_reaped_at_sweep(tmp_path: Path) -> None:
    """Decision 10 reaping confirmation: the asyncio child watcher observes the
    exit eagerly; the next sweep resolves it (no zombie until invocation end).
    If this ever fails, the spec's fallback is a per-process wait() supervisor
    — do not relax the assertion."""
    async def main() -> None:
        from imp.agents.tools.bg import _sweep_bg
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "true")
        bp = ctx.background_processes[out["id"]]
        with anyio.fail_after(10):
            while bp.proc.returncode is None:
                await anyio.sleep(0.05)
        await _sweep_bg(ctx)
        assert bp.status == "exited"
        assert bp.returncode == 0
    anyio.run(main)


def test_cap_breach_kills_group_and_warns_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def main() -> None:
        from imp.agents.tools import limits
        from imp.agents.tools.bg import _sweep_bg
        monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
        ctx = _ctx(tmp_path)
        log = _RecLog()
        ctx.event_log = log
        out = await _spawn(ctx, FLOODER)
        try:
            await _sweep_until_killed(ctx, out["id"])
            bp = ctx.background_processes[out["id"]]
            assert bp.kill_reason == "output-cap"
            [w] = log.warnings()
            # Decision 6: human-readable message (narrative renders payload.message)
            assert out["id"] in w["message"] and "output cap" in w["message"]
            assert w["id"] == out["id"]
            assert w["reason"] == "output-cap"
            assert len(w["command"]) <= 200
            # repeated-breach gate: files persist over-cap — later sweeps must NOT
            # re-kill or re-emit
            for _ in range(3):
                await _sweep_bg(ctx)
            assert len(log.warnings()) == 1
            # prior output still pageable after the kill
            raw = await get_registry()["bash_output"].async_invoke(
                ctx, {"id": out["id"]})
            r = json.loads(raw)
            assert r["status"] == "killed" and r["kill_reason"] == "output-cap"
            assert len(r["stdout"]) > 0
        finally:
            await _cleanup(ctx)
    anyio.run(main)


def test_agent_killed_flooder_not_relabeled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """kill_reason-is-None gate: an over-cap flooder the agent already killed
    keeps kill_reason 'agent' — no re-kill, no spurious warning."""
    async def main() -> None:
        from imp.agents.tools import limits
        from imp.agents.tools.bg import _sweep_bg
        monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 1)   # everything over-cap
        ctx = _ctx(tmp_path)
        log = _RecLog()
        ctx.event_log = log
        out = await _spawn(ctx, "echo data; sleep 60")
        try:
            await get_registry()["bash_kill"].async_invoke(ctx, {"id": out["id"]})
            bp = ctx.background_processes[out["id"]]
            assert bp.kill_reason == "agent"
            await _sweep_bg(ctx)
            assert bp.kill_reason == "agent"     # not relabeled "output-cap"
            assert log.warnings() == []
        finally:
            await _cleanup(ctx)
    anyio.run(main)


def test_sweep_tolerates_teardown_deleting_files(tmp_path: Path) -> None:
    async def main() -> None:
        from imp.agents.tools.bg import _sweep_bg
        ctx = _ctx(tmp_path)
        out = await _spawn(ctx, "sleep 60")
        bp = ctx.background_processes[out["id"]]
        bp.out_path.unlink()                 # teardown owns it now
        await _sweep_bg(ctx)                 # must not raise
        await _cleanup(ctx)
    anyio.run(main)


def test_forking_server_exited_leader_still_cap_killed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regardless-of-status rule: the leader exits, a forked child inherits the
    write fd and keeps flooding — the exited entry stays in the cap-stat set
    and breach kills the (still-live) group."""
    async def main() -> None:
        from imp.agents.tools import limits
        monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
        ctx = _ctx(tmp_path)
        ctx.event_log = _RecLog()
        out = await _spawn(ctx, f"( {FLOODER} ) & exit 0")
        try:
            bp = ctx.background_processes[out["id"]]
            with anyio.fail_after(10):
                while bp.proc.returncode is None:
                    await anyio.sleep(0.05)
            await _sweep_until_killed(ctx, out["id"])
            assert bp.kill_reason == "output-cap"
            assert bp.returncode == 0            # leader's REAL returncode retained
            # the child stopped writing: file growth halts
            size1 = os.stat(bp.out_path).st_size
            await anyio.sleep(0.5)
            assert os.stat(bp.out_path).st_size == size1
        finally:
            await _cleanup(ctx)
    anyio.run(main)


def test_breach_kill_proceeds_when_warning_emit_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raising event-log consumer must not skip the breach kill (the
    kill_reason gate means a skipped kill would never be retried) nor crash
    the sweep's host task group."""
    async def main() -> None:
        from imp.agents.tools import limits
        monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
        ctx = _ctx(tmp_path)

        class _BoomLog:
            def append(self, **kw):
                raise RuntimeError("hooked consumer blew up")

        ctx.event_log = _BoomLog()
        out = await _spawn(ctx, FLOODER)
        try:
            await _sweep_until_killed(ctx, out["id"])
            bp = ctx.background_processes[out["id"]]
            assert bp.status == "killed"
            assert bp.kill_reason == "output-cap"
        finally:
            await _cleanup(ctx)
    anyio.run(main)


def test_quiet_foreground_bash_still_sweeps_via_ticker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A background flooder + a silent long foreground bash (no chunks, then
    proc.wait()): the per-call ticker must cap-kill the flooder within ~1 s —
    a drain-site sweep could never fire across a quiet command."""
    async def main() -> None:
        from imp.agents.tools import limits
        monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
        ctx = _ctx(tmp_path)
        ctx.event_log = _RecLog()
        out = await _spawn(ctx, FLOODER)
        try:
            await get_registry()["bash"].async_invoke(ctx, {"command": "sleep 2.5"})
            bp = ctx.background_processes[out["id"]]
            assert bp.status == "killed"
            assert bp.kill_reason == "output-cap"
        finally:
            await _cleanup(ctx)
    anyio.run(main)


def test_ticker_no_regression_baseline(tmp_path: Path) -> None:
    """A normal, quick foreground bash returns promptly with complete output —
    no hang to the wall clock (uncancelled ticker), no truncated drain tail
    (whole-task-group cancel)."""
    async def main() -> None:
        ctx = _ctx(tmp_path)
        t0 = anyio.current_time()
        raw = await get_registry()["bash"].async_invoke(
            ctx, {"command": "echo head; sleep 0.2; echo tail"})
        out = json.loads(raw)
        assert anyio.current_time() - t0 < 5.0
        assert out["returncode"] == 0
        assert "head" in out["stdout"] and "tail" in out["stdout"]
    anyio.run(main)


def test_no_ticker_started_when_no_background_processes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gating: with an empty background registry, the per-call ticker is never
    started, so _sweep_bg is not invoked across a quiet foreground command that
    outlives the 1 s tick interval. Before the gate the ticker fired _sweep_bg
    once at ~1 s; the gate must drop that path entirely for the no-bg case."""
    async def main() -> None:
        import imp.agents.tools.bg as bg_mod
        ctx = _ctx(tmp_path)
        assert ctx.background_processes == {}
        calls = 0
        orig = bg_mod._sweep_bg

        async def _counting_sweep(c):
            nonlocal calls
            calls += 1
            return await orig(c)

        # _bg_ticker imports _sweep_bg from the module at call time, so patching
        # the module attribute is picked up if (and only if) the ticker runs.
        monkeypatch.setattr(bg_mod, "_sweep_bg", _counting_sweep)
        raw = await get_registry()["bash"].async_invoke(
            ctx, {"command": "sleep 1.2"})
        out = json.loads(raw)
        assert out["returncode"] == 0
        assert calls == 0
    anyio.run(main)


def test_sweep_fires_on_non_bash_tool_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An agent that backgrounds a flooder and then spends the invocation in
    read_file must still be swept: the dispatch-site sweep lives in the shared
    helper, so EVERY tool dispatch sweeps."""
    async def main() -> None:
        from imp.agents.dispatch import TurnState, dispatch_one_tool_use
        from imp.agents.runner import ToolUseBlock
        from imp.agents.tools import get_registry, limits
        monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
        ctx = _ctx(tmp_path)
        ctx.event_log = _RecLog()
        (tmp_path / "f.txt").write_text("data")
        out = await _spawn(ctx, FLOODER)
        try:
            class _Inv:
                class agent_def:
                    output_schema = None
            bp = ctx.background_processes[out["id"]]
            with anyio.fail_after(30):
                while bp.status != "killed":
                    await dispatch_one_tool_use(
                        ctx=ctx, elog=ctx.event_log, aid="step:t", invocation=_Inv(),
                        tools=get_registry(),
                        tu=ToolUseBlock(id="u1", name="read_file",
                                        input={"path": "f.txt"}),
                        tool_results=[], state=TurnState(tool_calls_used=0),
                        ceiling=None)
                    await anyio.sleep(0.1)
            assert bp.kill_reason == "output-cap"
        finally:
            await _cleanup(ctx)
    anyio.run(main)
