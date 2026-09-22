from __future__ import annotations

from pathlib import Path
from unittest.mock import patch as _patch

import pytest

import imp.providers.codex_runner as cr
from imp.agents.runner import AgentInvocation, QuestContext
from imp.agents.tools.context import ToolContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import (
    AgentDef,
    OutputField,
    OutputSchema,
    SourceLoc,
)
from imp.providers.codex_mcp import RunState
from imp.providers.codex_rollout import RolloutCursor, RolloutReadings, RolloutUsage
from imp.providers.codex_runner import CodexAgentRunner, CodexSession
from tests.async_wait import wait_pgid_gone, wait_until
from tests.codex_fakes import (
    FakeCodex,
    FakeStreamedTurn,
    agent_message,
    mcp_call,
    thread_started,
    turn_completed,
)

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _seed_auth(tmp_path: Path) -> None:
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t",
        description="",
        tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC,
        ),
        prompt_template_source="",
        source_path=Path("a.md"),
        source=_SRC,
        tier=None,
    )


def _build_invocation(
    tmp_path: Path, *, event_log: object = None,
) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="you are a test agent",
        initial_messages=(),
        quest_context=QuestContext(
            title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
        event_log=event_log,
    )


@pytest.mark.anyio
async def test_run_yields_agent_result_when_complete_step_captured(
    tmp_path: Path,
) -> None:
    _seed_auth(tmp_path)
    canonical = tmp_path / "auth.json"
    elog = InMemoryEventLog()

    scripted = [[
        thread_started("thr-1"),
        agent_message("ok, calling complete_step"),
        mcp_call(
            name="complete_step",
            args={"answer": "42"},
            result="step complete",
            item_id="cs-1",
        ),
        turn_completed(input_tokens=100, output_tokens=50),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = canonical  # test seam

    inv = _build_invocation(tmp_path, event_log=elog)

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    kinds = [e.get("type") for e in events]
    assert "agent_result" in kinds, events
    out = next(e for e in events if e.get("type") == "agent_result")
    assert out["payload"] == {"answer": "42"}


@pytest.mark.anyio
async def test_drive_turns_sweeps_bg_per_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-API-site analog: one sweep per streamed SDK event, so a
    backgrounded flooder is cap-checked across turns with no imp dispatch."""
    import imp.agents.tools.bg as bg_mod

    calls = {"n": 0}
    real = bg_mod._sweep_bg

    async def counting(ctx):
        calls["n"] += 1
        await real(ctx)

    monkeypatch.setattr(bg_mod, "_sweep_bg", counting)
    _seed_auth(tmp_path)
    canonical = tmp_path / "auth.json"
    elog = InMemoryEventLog()

    scripted = [[
        thread_started("thr-1"),
        agent_message("ok, calling complete_step"),
        mcp_call(
            name="complete_step",
            args={"answer": "42"},
            result="step complete",
            item_id="cs-1",
        ),
        turn_completed(input_tokens=100, output_tokens=50),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = canonical  # test seam

    inv = _build_invocation(tmp_path, event_log=elog)

    events = [e async for e in runner.run(inv)]
    assert events[-1]["type"] == "agent_result"
    assert calls["n"] >= 2


@pytest.mark.anyio
async def test_run_fails_fast_with_network_error_when_self_ping_fails(
    tmp_path: Path,
) -> None:
    """Spec run() flow step 2: imp pings its own MCP endpoint and fails fast
    with a `network` ProviderError when unreachable, BEFORE provisioning
    CODEX_HOME or starting any SDK turn."""
    _seed_auth(tmp_path)

    fake = FakeCodex(scripted_turns=[[]])  # won't get touched
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: fake,
    )
    runner._canonical_auth_override = tmp_path / "auth.json"
    inv = _build_invocation(tmp_path)

    async def _boom(**_kwargs: object) -> None:
        raise RuntimeError("connection refused (synthetic)")

    with _patch("imp.providers.codex_runner._ping_mcp_server", new=_boom):
        events = []
        async for ev in runner.run(inv):
            events.append(ev)

    assert events, "expected at least one yielded event"
    err = events[-1]
    assert err["type"] == "error"
    assert err["payload"]["category"] == "network"
    assert "ping failed" in err["payload"]["message"]
    # Codex was never started (fail-fast before SDK turn).
    assert fake.start_thread_calls == []


@pytest.mark.anyio
async def test_response_wait_is_request_bracketed(tmp_path: Path) -> None:
    import dataclasses

    from imp.cli.signals import InterruptCoordinator

    _seed_auth(tmp_path)
    canonical = tmp_path / "auth.json"
    elog = InMemoryEventLog()

    scripted = [[
        thread_started("thr-1"),
        agent_message("ok, calling complete_step"),
        mcp_call(
            name="complete_step", args={"answer": "42"},
            result="step complete", item_id="cs-1",
        ),
        turn_completed(input_tokens=100, output_tokens=50),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = canonical

    coord = InterruptCoordinator()
    # begin_request_watchdog snapshots request_active True just before each await
    # for the next streamed event.
    snaps: list[bool] = []
    coord.add_activity_listener(lambda: snaps.append(coord.request_active))

    inv = dataclasses.replace(
        _build_invocation(tmp_path, event_log=elog), coordinator=coord)
    events = [e async for e in runner.run(inv)]

    assert events[-1]["type"] == "agent_result"
    # request_bracketed arms request_active while awaiting each of the 4 streamed
    # events (thread.started, the agent_message item, the complete_step item,
    # turn.completed).
    assert sum(1 for s in snaps if s) >= 4
    assert coord.request_active is False


@pytest.mark.anyio
async def test_codex_gen_close_runs_teardown(tmp_path: Path) -> None:
    """Teardown site 2 under codex: aclosing run() mid-stream kills the
    backgrounded group via the new run()-finally (NOT _run_loop's existing
    finally — that one only GC-finalizes on abandonment, PEP 525).

    HARNESS APPROACH (cancellation; true-aclose not attempted):
    FakeCodex.mcp_call is REPORT-ONLY (confirmed: codex_mcp.dispatch_tool is
    invoked via the real MCP server, not via FakeCodex item scripting; mcp_call
    items are decoded by process_event for audit logging only, not dispatched to
    the registry bash). Therefore this test uses the same cancellation pattern
    as CC's sibling:

      (a) A stalling FakeCodex thread whose run_streamed() blocks via
          anyio.sleep_forever(), preventing _drive_turns from progressing
          past the first run_streamed() call.
      (b) The generator is driven in a task group; the main task polls until
          runner._ctx is set (set synchronously before provisioning), then
          directly spawns a bg process via _spawn_bg.
      (c) The task group is cancelled, propagating CancelledError through
          run()'s async-for and firing the try/finally teardown block.

    True aclose() was not pursued because the FakeCodex event loop exhausts
    synchronously (no yield point in _AsyncIterEvents.__anext__), making it
    impossible to break at a mid-stream yield and call gen.aclose() from the
    same task. Cancellation is equivalent for the finally assertion.

    Construction mirrored from test_run_yields_agent_result_when_complete_step_captured
    (same file): _seed_auth, _canonical_auth_override, FakeCodex
    client_factory, _build_invocation — adapted to add bash to agent_def.tools.
    """
    import anyio as _anyio

    from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
    from tests.test_bg_cc_codex_parity import _cleanup, _spawn_bg

    _SRC_LOCAL = SourceLoc(file=Path("a.md"), line=1)

    class _StallingThread:
        """FakeThread whose run_streamed() stalls until cancelled — keeps the
        runner alive in _drive_turns long enough for the main task to spawn a
        bg process and then cancel the task group."""
        async def run_streamed(self, prompt: object, options: object = None) -> object:
            await _anyio.sleep_forever()  # stall until task group is cancelled

    class _StallingFakeCodex:
        """Client that returns a _StallingThread on start_thread()."""
        def start_thread(self, options: object = None) -> _StallingThread:
            return _StallingThread()

    _seed_auth(tmp_path)
    canonical = tmp_path / "auth.json"

    # Build invocation with bash in tools so bg injection fires and
    # _spawn_bg can be used to start a real background process.
    bash_agent_def = AgentDef(
        name="t",
        description="",
        tools=frozenset({"complete_step", "bash"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="answer", type="string", required=True),),
            source=_SRC_LOCAL,
        ),
        prompt_template_source="",
        source_path=Path("a.md"),
        source=_SRC_LOCAL,
        tier=None,
    )

    from imp.agents.runner import AgentInvocation, QuestContext
    from imp.engine.state import InMemoryEventLog

    elog = InMemoryEventLog()
    inv = AgentInvocation(
        agent_def=bash_agent_def,
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="you are a test agent",
        initial_messages=(),
        quest_context=QuestContext(
            title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
        event_log=elog,
    )

    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: _StallingFakeCodex(),
    )
    runner._canonical_auth_override = canonical

    gen = runner.run(inv)
    bg_pid: int | None = None
    bg_id: str | None = None
    ctx_seen = None

    try:
        async with _anyio.create_task_group() as tg:
            async def _drive() -> None:
                async for _ev in gen:
                    pass  # pragma: no cover

            tg.start_soon(_drive)
            # _ctx is set in _run_loop after preflight and ToolContext
            # construction, before the MCP server ping; poll until it appears.
            await wait_until(lambda: runner._ctx is not None)
            ctx_seen = runner._ctx
            out = await _spawn_bg(runner._ctx)
            bg_pid = out["pid"]
            bg_id = out["id"]
            # Cancel the drive task — propagates through run()'s finally.
            tg.cancel_scope.cancel()

        # run()'s finally must have called terminate_background_processes.
        assert ctx_seen is not None
        assert bg_pid is not None
        await wait_pgid_gone(bg_pid)
        assert bg_id is not None
        bp = ctx_seen.background_processes[bg_id]
        assert bp.status == "killed" and bp.kill_reason == "interrupted"
    finally:
        if ctx_seen is not None:
            await _cleanup(ctx_seen)


@pytest.mark.anyio
async def test_codex_bridges_stderr_warning_into_runner_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real _run_loop builds its sink via build_stderr_sink and closes it in
    the finally. Wrapping the factory to feed a Warning line proves codex wires the
    bridge with source="codex" and that close() emits the runner_warning."""
    import imp.providers.stderr_sink as ss

    _seed_auth(tmp_path)
    canonical = tmp_path / "auth.json"
    elog = InMemoryEventLog()

    real_build = ss.build_stderr_sink

    def _wrap(path: object, **kw: object) -> object:
        sink = real_build(path, **kw)  # type: ignore[arg-type]
        sink.feed("codex: Warning: nested config drift detected\n")
        return sink

    # _run_loop does a function-local `from imp.providers.stderr_sink import
    # build_stderr_sink`, which resolves the module attribute at call time.
    monkeypatch.setattr(ss, "build_stderr_sink", _wrap)

    scripted = [[
        thread_started("thr-1"),
        agent_message("ok, calling complete_step"),
        mcp_call(
            name="complete_step", args={"answer": "42"},
            result="step complete", item_id="cs-1",
        ),
        turn_completed(input_tokens=100, output_tokens=50),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = canonical
    inv = _build_invocation(tmp_path, event_log=elog)

    _events = [e async for e in runner.run(inv)]

    warns = [e for e in elog.read() if e["type"] == "runner_warning"]
    assert any(
        e["payload"]["message"] == "codex stderr: Warning: nested config drift detected"
        for e in warns), warns


@pytest.mark.anyio
async def test_codex_run_aclose_cascades_to_run_agent_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing run() mid-stream must synchronously close the inner run_agent_loop
    generator (contextlib.aclosing), not abandon it to GC finalization."""
    closed: list[bool] = []

    async def _fake_loop(invocation: object):  # type: ignore[no-untyped-def]
        try:
            yield {"type": "a"}
            yield {"type": "b"}
        finally:
            closed.append(True)

    runner = CodexAgentRunner(model_name="gpt-5.5")
    monkeypatch.setattr("imp.providers.codex_runner.run_agent_loop",
                        lambda invocation, session: _fake_loop(invocation))

    gen = runner.run(_build_invocation(tmp_path))
    first = await gen.__anext__()
    assert first == {"type": "a"}
    await gen.aclose()
    # Deterministic: the finally ran INSIDE aclose(), not on a later GC pass.
    assert closed == [True]


@pytest.mark.anyio
async def test_session_real_resolver_forwards_state_before_event_emission(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class _ScriptedThread:
        def __init__(self) -> None:
            self.turns = [
                [thread_started("thr-1"), agent_message("first"),
                 turn_completed(input_tokens=11, cached_input_tokens=3,
                                cache_write_input_tokens=1, output_tokens=5,
                                reasoning_output_tokens=2)],
                [agent_message("second"),
                 turn_completed(input_tokens=22, cached_input_tokens=6,
                                cache_write_input_tokens=2, output_tokens=10,
                                reasoning_output_tokens=4)],
            ]
            self.order: list[str] = []

        async def run_streamed(self, prompt: object,
                               options: object = None) -> FakeStreamedTurn:
            self.order.append(f"run:{prompt}")
            return FakeStreamedTurn(self.turns.pop(0))

    class _Telemetry:
        def __init__(self) -> None:
            self.thread_id: str | None = None
            self.begin_thread_ids: list[str | None] = []
            self.cursors = [
                RolloutCursor((), discovery_complete=True),
                RolloutCursor((), discovery_complete=False),
            ]

        def begin_turn(self) -> RolloutCursor:
            self.begin_thread_ids.append(self.thread_id)
            thread.order.append(f"begin:{self.thread_id}")
            return self.cursors.pop(0)

    class _Task:
        def done(self) -> bool:
            return False

    class _ServerHandle:
        task = _Task()

    thread = _ScriptedThread()
    telemetry = _Telemetry()
    expected_cursors = list(telemetry.cursors)
    resolved: list[tuple[object, RolloutCursor, RolloutUsage]] = []
    returned = [
        RolloutReadings(context=(111, 1000), quota=None),
        RolloutReadings(context=(222, 1000), quota=None),
    ]

    async def wait(current_telemetry: object, cursor: RolloutCursor,
                   usage: RolloutUsage) -> RolloutReadings:
        resolved.append((current_telemetry, cursor, usage))
        return returned.pop(0)

    monkeypatch.setattr(cr, "_wait_for_current_readings", wait)
    elog = InMemoryEventLog()
    invocation = _build_invocation(tmp_path, event_log=elog)
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._server_handle = _ServerHandle()
    runner._codex_home = tmp_path
    session = CodexSession(invocation, runner)
    session.elog = elog
    session.aid = "step:t"
    session.ctx = ToolContext(project_root=tmp_path,
                              events_path=tmp_path / "events.jsonl")
    session._state = RunState(cap=100, agent_id="step:t")
    session._thread = thread
    session._next_prompt = "first"
    session._requery_pending = False
    session._proc_state = {
        "thread_id": None,
        "rollout_telemetry": telemetry,
        "rollout_cursor": RolloutCursor((), discovery_complete=True),
        "auth_mode": "chatgpt",
        "context_reading": None,
        "context_canary_emitted": False,
        "quota_reading": None,
        "turn_output_tokens": None,
        "compaction_canary_emitted": False,
        "turn_tool_outcomes": [],
        "last_agent_text": "",
    }

    async def drive_turn() -> None:
        async for raw in session._receive():
            async for _ in session._message_to_items(raw):
                pass

    await drive_turn()
    session._next_prompt = "second"
    await drive_turn()

    assert thread.order == ["begin:None", "run:first", "begin:thr-1", "run:second"]
    assert telemetry.begin_thread_ids == [None, "thr-1"]
    assert [cursor for _, cursor, _ in resolved] == expected_cursors
    assert expected_cursors[0] != expected_cursors[1]
    assert [current for current, _, _ in resolved] == [telemetry, telemetry]
    assert [usage for _, _, usage in resolved] == [
        RolloutUsage(11, 3, 1, 5, 2),
        RolloutUsage(22, 6, 2, 10, 4),
    ]
    messages = [event["payload"] for event in elog.read()
                if event["type"] == "agent_message"]
    assert [(message["context_tokens"], message["context_window"])
            for message in messages] == [(111, 1000), (222, 1000)]


@pytest.mark.anyio
async def test_receive_aclose_cascades_to_events(tmp_path: Path) -> None:
    """Closing CodexSession._receive mid-stream must synchronously aclose() the
    inner request_bracketed(streamed.events) generator via _receive's aclosing
    wrapper — the site-3 cascade. Falsifiable: without the aclosing wrapper,
    async-for does not auto-close its iterator on GeneratorExit, so
    aclose_called stays [] and the test fails."""
    import types

    from imp.agents.tools.context import ToolContext
    from imp.engine.state import InMemoryEventLog
    from imp.providers.codex_rollout import RolloutCursor, RolloutTelemetry
    from imp.providers.codex_runner import _SERVER_DIED, CodexSession

    aclose_called: list[bool] = []

    class _FakeEvents:
        def __aiter__(self) -> _FakeEvents:
            return self

        async def __anext__(self) -> object:
            return types.SimpleNamespace(type="thread.started")

        async def aclose(self) -> None:
            aclose_called.append(True)

    class _FakeStreamed:
        events = _FakeEvents()

    class _FakeThread:
        async def run_streamed(self, prompt: object, options: object = None) -> object:
            return _FakeStreamed()

    class _FakeTask:
        def done(self) -> bool:
            return True

    class _FakeServerHandle:
        task = _FakeTask()

    elog = InMemoryEventLog()
    inv = _build_invocation(tmp_path, event_log=elog)
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._server_handle = _FakeServerHandle()
    session = CodexSession(inv, runner)
    session.elog = elog
    session.aid = "step:t"
    session.ctx = ToolContext(project_root=tmp_path,
                              events_path=tmp_path / "events.jsonl")
    session._thread = _FakeThread()
    session._next_prompt = "go"
    session._proc_state = {
        "rollout_telemetry": RolloutTelemetry(tmp_path, None),
        "rollout_cursor": RolloutCursor((), discovery_complete=True),
        "thread_id": None,
    }

    gen = session._receive()
    first = await gen.__anext__()
    assert first is _SERVER_DIED
    await gen.aclose()
    assert aclose_called == [True]


@pytest.mark.anyio
async def test_run_finally_persists_rollout_before_teardown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import imp.providers.codex_runner as _cr

    order: list[str] = []

    async def _fake_loop(invocation, session):
        session.runner._codex_home = tmp_path / "ch"
        session.runner._canonical_auth = tmp_path / "auth.json"
        session.runner._proc_state = {"thread_id": "t-1"}
        yield {"type": "agent_result", "payload": {"status": "Success"}}

    monkeypatch.setattr("imp.providers.codex_rollout.persist_rollout",
                        lambda **_kw: order.append("persist"))
    monkeypatch.setattr(_cr, "teardown_codex_home",
                        lambda **_kw: order.append("teardown"))
    monkeypatch.setattr(_cr, "run_agent_loop",
                        lambda invocation, session: _fake_loop(invocation, session))

    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._canonical_auth_override = tmp_path / "auth.json"
    _ = [ev async for ev in runner.run(_build_invocation(tmp_path))]
    assert order == ["persist", "teardown"]


@pytest.mark.anyio
async def test_run_finally_shreds_home_even_if_rollout_persist_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rollout-persist IO failure must NOT skip the auth-shredding
    teardown_codex_home, and must NOT propagate out of the finally."""
    import imp.providers.codex_runner as _cr

    calls: list[str] = []

    async def _fake_loop(invocation, session):
        session.runner._codex_home = tmp_path / "ch"
        session.runner._canonical_auth = tmp_path / "auth.json"
        session.runner._proc_state = {"thread_id": "t-1"}
        yield {"type": "agent_result", "payload": {"status": "Success"}}

    def _raising_persist(**_kw: object) -> None:
        calls.append("persist")
        raise OSError("rollout JSONL write failed")

    monkeypatch.setattr("imp.providers.codex_rollout.persist_rollout",
                        _raising_persist)
    monkeypatch.setattr(_cr, "teardown_codex_home",
                        lambda **_kw: calls.append("teardown"))
    monkeypatch.setattr(_cr, "run_agent_loop",
                        lambda invocation, session: _fake_loop(invocation, session))

    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._canonical_auth_override = tmp_path / "auth.json"
    events = [ev async for ev in runner.run(_build_invocation(tmp_path))]
    assert calls == ["persist", "teardown"]
    assert events[-1] == {"type": "agent_result", "payload": {"status": "Success"}}


@pytest.mark.anyio
async def test_turn_boundary_closes_event_stream_before_next_turn(
    tmp_path: Path,
) -> None:
    """The full teardown cascade under codex: turn 0's event stream is closed
    before turn 1's run_streamed, with no intervening event-loop tick for the
    async-generator finalizer hook to exploit. Falsifiable in both directions —
    reverting EITHER aclosing wrap (run_agent_loop's events(), ObserveSession's
    _receive()) leaves the close un-recorded at the boundary."""
    import dataclasses

    _seed_auth(tmp_path)
    elog = InMemoryEventLog()
    order: list[str] = []

    scripted = [
        [thread_started("thr-1"),
         agent_message("still working on it"),
         turn_completed(input_tokens=10, output_tokens=5)],
        [agent_message("done now"),
         mcp_call(name="complete_step", args={"answer": "42"},
                  result="step complete", item_id="cs-1"),
         turn_completed(input_tokens=20, output_tokens=7)],
    ]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(
            scripted_turns=scripted,
            on_turn_start=lambda: order.append("run_streamed"),
            on_events_aclose=lambda: order.append("aclose")),
    )
    runner._canonical_auth_override = tmp_path / "auth.json"  # test seam
    inv = dataclasses.replace(
        _build_invocation(tmp_path, event_log=elog),
        continuation_fn=lambda last: ("keep going", False))

    events = [e async for e in runner.run(inv)]

    assert events[-1]["type"] == "agent_result", events
    assert events[-1]["payload"] == {"answer": "42"}
    assert order == ["run_streamed", "aclose", "run_streamed", "aclose"]
