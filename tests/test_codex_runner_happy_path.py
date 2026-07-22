from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch as _patch

import pytest

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import (
    AgentDef,
    OutputField,
    OutputSchema,
    SourceLoc,
)
from imp.providers.codex_runner import CodexAgentRunner
from tests.async_wait import wait_until
from tests.codex_fakes import (
    FakeCodex,
    agent_message,
    mcp_call,
    thread_started,
    turn_completed,
)

_SRC = SourceLoc(file=Path("a.md"), line=1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(
        ["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(
        ["git", "-C", str(p), "config", "user.name", "T"], check=True)
    # An initial commit so `git stash create` (the snapshot substrate) has a
    # HEAD to work against.
    (p / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(p), "add", ".gitkeep"], check=True)
    subprocess.run(
        ["git", "-C", str(p), "commit", "-q", "-m", "init"], check=True)


def _seed_git_and_auth(tmp_path: Path) -> None:
    _git_init(tmp_path)
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
    _seed_git_and_auth(tmp_path)
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
    _seed_git_and_auth(tmp_path)
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
    _seed_git_and_auth(tmp_path)

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

    _seed_git_and_auth(tmp_path)
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
    (same file): _seed_git_and_auth, _canonical_auth_override, FakeCodex
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

    _seed_git_and_auth(tmp_path)
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
        import os
        assert ctx_seen is not None
        assert bg_pid is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(bg_pid, 0)
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

    _seed_git_and_auth(tmp_path)
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
async def test_codex_run_aclose_cascades_to_run_loop(tmp_path: Path) -> None:
    """Closing run() mid-stream must synchronously close the inner _run_loop
    generator (contextlib.aclosing), not abandon it to GC finalization."""
    closed: list[bool] = []

    async def _fake_loop(invocation: object):  # type: ignore[no-untyped-def]
        try:
            yield {"type": "a"}
            yield {"type": "b"}
        finally:
            closed.append(True)

    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._run_loop = _fake_loop  # type: ignore[assignment]  # test seam: bypass preflight/MCP

    gen = runner.run(_build_invocation(tmp_path))
    first = await gen.__anext__()
    assert first == {"type": "a"}
    await gen.aclose()
    # Deterministic: the finally ran INSIDE aclose(), not on a later GC pass.
    assert closed == [True]


@pytest.mark.anyio
async def test_run_loop_aclose_cascades_to_drive_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Closing run() mid-stream must synchronously close the inner _drive_turns
    generator via _run_loop's contextlib.aclosing wrapper (site 2).

    All heavy preflight seams are monkeypatched to lightweight in-memory fakes so
    no real MCP server, socket, subprocess, or filesystem access is needed.
    runner._drive_turns is replaced with a fake async generator whose finally
    records closure; the test asserts that finally ran deterministically inside
    gen.aclose(), not on a later GC pass.
    """
    from imp.engine.state import InMemoryEventLog
    from imp.providers.codex_runner import EntrySnapshot

    drive_turns_closed: list[bool] = []

    async def _fake_drive_turns(**_kw: object) -> object:  # type: ignore[misc]
        try:
            yield {"type": "from_drive_turns"}
        finally:
            drive_turns_closed.append(True)

    class _FakeServerHandle:
        url = "http://127.0.0.1:1"
        host = "127.0.0.1"
        port = 1

        async def shutdown(self) -> None:
            pass

    class _FakeStderrSink:
        def close(self) -> None:
            pass

        def tail(self) -> str:
            return ""

    async def _fake_start_server(**_kw: object) -> _FakeServerHandle:
        return _FakeServerHandle()

    async def _fake_ping(**_kw: object) -> None:
        pass

    import imp.providers.codex_runner as _cr
    import imp.providers.stderr_sink as _ss

    monkeypatch.setattr(_cr, "preflight", lambda **_kw: "api_key")
    monkeypatch.setattr(_cr, "quest_scratch_root", lambda: tmp_path)
    monkeypatch.setattr(_cr, "sweep_stale_codex_homes", lambda **_kw: None)
    monkeypatch.setattr(_cr, "take_snapshot", lambda _cwd: EntrySnapshot(stash_sha=""))
    monkeypatch.setattr(_cr, "start_server", _fake_start_server)
    monkeypatch.setattr(_ss, "build_stderr_sink", lambda *_a, **_kw: _FakeStderrSink())
    monkeypatch.setattr(_cr, "_ping_mcp_server", _fake_ping)
    monkeypatch.setattr(_cr, "provision_codex_home", lambda **_kw: tmp_path / "ch")
    monkeypatch.setattr(_cr, "seed_auth", lambda **_kw: None)
    monkeypatch.setattr(_cr, "teardown_codex_home", lambda **_kw: None)

    elog = InMemoryEventLog()
    inv = _build_invocation(tmp_path, event_log=elog)
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._canonical_auth_override = tmp_path / "auth.json"
    runner._client_factory = lambda options=None: object()
    runner._drive_turns = _fake_drive_turns  # type: ignore[assignment]

    gen = runner.run(inv)
    first = await gen.__anext__()
    assert first == {"type": "from_drive_turns"}
    await gen.aclose()
    # Deterministic: aclosing(_drive_turns) in _run_loop closed it inside aclose().
    assert drive_turns_closed == [True]


@pytest.mark.anyio
async def test_drive_turns_aclose_cascades_to_events(tmp_path: Path) -> None:
    """Closing _drive_turns mid-stream must synchronously aclose() the inner
    request_bracketed(streamed.events) generator via the aclosing wrapper at
    codex_runner.py:670 — proving the site-3 cascade.

    Mechanism: _drive_turns yields the mcp_server_died error (dead-server check
    at line 676), suspending at line 679 INSIDE the
    `async with aclosing(request_bracketed(...)) as _events` block with _events
    still open. gen.aclose() throws GeneratorExit there; the aclosing
    wrapper's __aexit__ deterministically calls _events.aclose() →
    request_bracketed.finally → streamed.events.aclose().

    Falsifiable: without the aclosing wrapper, async-for does not auto-close its
    iterator on GeneratorExit, so aclose_called stays [] and the test fails.
    """
    import types

    from imp.agents.tools.context import ToolContext
    from imp.engine.state import InMemoryEventLog
    from imp.providers.codex_mcp import RunState
    from imp.providers.codex_runner import EntrySnapshot

    aclose_called: list[bool] = []

    class _FakeEvents:
        def __aiter__(self) -> _FakeEvents:
            return self

        async def __anext__(self) -> object:
            return types.SimpleNamespace(type="thread.started")

        async def aclose(self) -> None:
            aclose_called.append(True)

    class _FakeThread:
        async def run_streamed(
            self, prompt: object, options: object = None,
        ) -> object:
            class _Streamed:
                events = _FakeEvents()

            return _Streamed()

    class _FakeClient:
        def start_thread(self, options: object = None) -> _FakeThread:
            return _FakeThread()

    class _FakeTask:
        def done(self) -> bool:
            return True

        def cancelled(self) -> bool:
            return False

        def exception(self) -> object:
            return None

    class _FakeServerHandle:
        task = _FakeTask()

    elog = InMemoryEventLog()
    inv = _build_invocation(tmp_path, event_log=elog)
    runner = CodexAgentRunner(model_name="gpt-5.5")

    gen = runner._drive_turns(
        client=_FakeClient(),
        invocation=inv,
        elog=elog,
        aid="step:t",
        ctx=ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl"),
        state=RunState(cap=1000, agent_id="step:t"),
        proc_state={},
        entry_snapshot=EntrySnapshot(stash_sha=""),
        server_handle=_FakeServerHandle(),
        codex_home=tmp_path,
    )
    # Pull the first event: _drive_turns enters the async for, gets one event
    # from _FakeEvents, hits the dead-server check (server_handle.task.done()),
    # and yields the mcp_server_died error — suspending at line 679 inside the
    # aclosing(request_bracketed(...)) block with _events still open.
    first = await gen.__anext__()
    assert first["type"] == "error"
    assert first["payload"]["kind"] == "mcp_server_died"

    # gen.aclose() throws GeneratorExit at the suspended yield; the aclosing
    # wrapper calls _events.aclose() → request_bracketed.finally → leaf.aclose().
    await gen.aclose()
    assert aclose_called == [True]
