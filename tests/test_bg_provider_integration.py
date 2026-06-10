# tests/test_bg_provider_integration.py
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from imp.engine.state import InMemoryEventLog
from imp.providers import anthropic as anthropic_mod
from imp.providers import openai as openai_mod
from imp.providers.anthropic import AnthropicAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic, response, tool_use
from tests.test_build_tools_wait_for_user import _Inv
from tests.test_heartbeat import _inv

IN_PROCESS = [anthropic_mod, openai_mod]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.parametrize("provider", IN_PROCESS)
def test_bg_tools_injected_with_bash(provider) -> None:
    tools = provider._build_tools(_Inv(["complete_step", "bash"]))
    assert "bash_output" in tools and "bash_kill" in tools


@pytest.mark.parametrize("provider", IN_PROCESS)
def test_bg_tools_absent_without_bash(provider) -> None:
    tools = provider._build_tools(_Inv(["complete_step", "read_file"]))
    assert "bash_output" not in tools and "bash_kill" not in tools


def _spawn_response(command: str = "sleep 60"):
    return response(tool_use("u1", "bash", {"command": command,
                                            "run_in_background": True}))


def _bg_paths(tmp_path: Path) -> list[Path]:
    bg_root = tmp_path / "bg"     # events_path is tmp_path/events.jsonl
    if not bg_root.is_dir():
        return []
    return [p for d in bg_root.iterdir() for p in d.iterdir()]


def _spawn_result(log: InMemoryEventLog) -> dict:
    # append_tool_result persists payload {"id", "content", "error"}
    # (state.py:297-309) — "content" carries the tool's JSON string.
    tr = [e for e in log.read() if e["type"] == "tool_result"]
    return json.loads(tr[0]["payload"]["content"])


@pytest.mark.anyio
async def test_gen_close_runs_teardown_before_redrive(tmp_path: Path) -> None:
    """Teardown site 3: abandoning the generator (error/backoff re-drive,
    pause) must kill attempt 1's processes and remove its stream files NOW —
    via run()'s finally — not at GC."""
    fake = FakeAsyncAnthropic([_spawn_response()],
                              default_response=_spawn_response())
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)
    inv = _inv(tmp_path, {"complete_step", "bash"}, event_log=log)
    gen = runner.run(inv)
    # The turn's FIRST heartbeat is the post-API one, yielded BEFORE the
    # dispatch loop — the spawn has happened only once the per-tu heartbeat
    # (the second) arrives.
    hb = 0
    async for ev in gen:
        if ev["type"] == "tool_heartbeat":
            hb += 1
            if hb == 2:                             # post-API, spawn, per-tu
                break
    await gen.aclose()
    spawn = _spawn_result(log)
    with pytest.raises(ProcessLookupError):
        os.killpg(spawn["pid"], 0)                  # attempt 1's group is dead
    leftover = [p.name for p in _bg_paths(tmp_path)]
    assert leftover == ["tombstones.json"]          # stream files removed

    # The retried invocation (fresh runner, same agent_id) seeds: first spawn
    # is bg2, and bg1 answers from the tombstone.
    fake2 = FakeAsyncAnthropic([
        _spawn_response(),
        response(tool_use("u2", "bash_output", {"id": "bg1"})),
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    log2 = InMemoryEventLog()
    runner2 = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake2)
    # Same tmp_path => same events_path + agent_id => same scratch dir (the
    # seeding scenario); a DIFFERENT transcript_dir so the re-drive starts a
    # fresh conversation instead of resuming attempt 1's transcript.
    inv2 = _inv(tmp_path, {"complete_step", "bash"}, event_log=log2,
                transcript_dir="step2")
    events = [e async for e in runner2.run(inv2)]
    results = [e for e in log2.read() if e["type"] == "tool_result"]
    spawn2 = json.loads(results[0]["payload"]["content"])
    assert spawn2["id"] == "bg2"
    tomb = json.loads(results[1]["payload"]["content"])
    assert tomb["status"] == "killed" and tomb["kill_reason"] == "interrupted"
    assert events[-1]["type"] == "agent_output"


@pytest.mark.anyio
async def test_early_aclose_with_none_ctx_is_safe(tmp_path: Path) -> None:
    """run()'s finally fires with self._ctx None when the generator is aclosed
    before its first pull — must not AttributeError."""
    runner = AnthropicAgentRunner(model_name="m",
                                  client_factory=lambda: FakeAsyncAnthropic([]))
    gen = runner.run(_inv(tmp_path, {"complete_step", "bash"}))
    await gen.aclose()                              # no error


@pytest.mark.anyio
async def test_interrupt_tears_down_background(tmp_path: Path) -> None:
    """Decision 7: pause kills background processes; tombstones explain it."""
    fake = FakeAsyncAnthropic([_spawn_response()],
                              default_response=_spawn_response("sleep 61"))
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)
    gen = runner.run(_inv(tmp_path, {"complete_step", "bash"}, event_log=log))
    hb = 0
    async for ev in gen:
        if ev["type"] == "tool_heartbeat":
            hb += 1
            if hb == 2:        # second heartbeat = spawn's per-tu heartbeat
                break
    await runner.interrupt()
    spawn = _spawn_result(log)
    with pytest.raises(ProcessLookupError):
        os.killpg(spawn["pid"], 0)
    # drain the cancelled generator, then close
    async for _ in gen:
        pass
    await gen.aclose()


@pytest.mark.anyio
async def test_bash_kill_emits_no_tool_killed(tmp_path: Path) -> None:
    """Decision 6: agent-requested kill is a normal result. The rc<0 heuristic
    is gated on tu.name == 'bash' and must never see bash_kill's result."""
    fake = FakeAsyncAnthropic([
        _spawn_response(),
        response(tool_use("u2", "bash_kill", {"id": "bg1"})),
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    log = InMemoryEventLog()
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)
    events = [e async for e in runner.run(
        _inv(tmp_path, {"complete_step", "bash"}, event_log=log))]
    assert events[-1]["type"] == "agent_output"
    assert "tool_killed" not in [e["type"] for e in log.read()]
    kill_result = json.loads(
        [e for e in log.read() if e["type"] == "tool_result"][1]["payload"]["content"])
    assert kill_result["returncode"] < 0            # real signal, plain data


@pytest.mark.anyio
async def test_post_api_sweep_runs_before_first_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []
    import imp.agents.tools.bg as bg_mod
    real_sweep = bg_mod._sweep_bg

    async def rec_sweep(ctx):
        order.append("sweep")
        await real_sweep(ctx)

    monkeypatch.setattr(bg_mod, "_sweep_bg", rec_sweep)
    import imp.agents.dispatch as dispatch_mod
    real_dispatch = dispatch_mod.dispatch_one_tool_use

    async def rec_dispatch(**kw):
        order.append("dispatch")
        return await real_dispatch(**kw)

    monkeypatch.setattr(anthropic_mod, "dispatch_one_tool_use", rec_dispatch)
    (tmp_path / "f.txt").write_text("x")
    fake = FakeAsyncAnthropic([
        response(tool_use("u1", "read_file", {"path": "f.txt"})),
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)
    [e async for e in runner.run(
        _inv(tmp_path, {"complete_step", "read_file"},
             event_log=InMemoryEventLog()))]
    assert order[0] == "sweep"                       # post-API sweep first
    assert "dispatch" in order
    assert order.index("sweep") < order.index("dispatch")


@pytest.mark.anyio
async def test_wired_providers_enable_background(tmp_path: Path) -> None:
    """The two in-process providers construct their ToolContext with
    background_enabled=True (the full surface is wired); the e2e spawn tests
    above only pass because of this — this pins the flag explicitly."""
    import re
    from pathlib import Path as _P
    for mod in ("anthropic", "openai"):
        src = _P(f"src/imp/providers/{mod}.py").read_text(encoding="utf-8")
        assert re.search(r"background_enabled=True", src), mod


@pytest.mark.anyio
async def test_cap_warning_reaches_hooked_event_log_from_wait_tick(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Live-path pin: a breach detected inside a bash_output(wait=True) tick
    emits through ctx.event_log — the on_event-HOOKED instance the executor
    threads — not a bare log that reaches only the file."""
    from imp.agents.tools import limits
    monkeypatch.setattr(limits, "BG_STREAM_FILE_CAP_BYTES", 50_000)
    hooked: list[str] = []
    log = InMemoryEventLog(on_event=lambda e: hooked.append(e["type"]))
    fake = FakeAsyncAnthropic([
        _spawn_response("while true; do echo xxxxxxxxxxxxxxxxxxxx; done"),
        response(tool_use("u2", "bash_output", {"id": "bg1", "wait": True,
                                                "timeout": 30})),
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    runner = AnthropicAgentRunner(model_name="m", client_factory=lambda: fake)
    events = [e async for e in runner.run(
        _inv(tmp_path, {"complete_step", "bash"}, event_log=log))]
    assert events[-1]["type"] == "agent_output"
    assert "runner_warning" in hooked                # rode the on_event hook
