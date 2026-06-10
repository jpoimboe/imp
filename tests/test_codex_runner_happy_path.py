from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch as _patch

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.state import InMemoryEventLog
from imp.engine.types import (
    AgentDef,
    FsRule,
    OutputField,
    OutputSchema,
    SourceLoc,
)
from imp.providers.codex_runner import CodexAgentRunner
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
        change_context=ChangeContext(
            slug="s", title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        fs_permissions=(
            FsRule(operations=("read", "write"), paths=("**",),
                   mode="allow", source=_SRC),
        ),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
        event_log=event_log,
    )


@pytest.mark.anyio
async def test_run_yields_agent_output_when_complete_step_captured(
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
    assert "agent_output" in kinds, events
    out = next(e for e in events if e.get("type") == "agent_output")
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
    assert events[-1]["type"] == "agent_output"
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

    Construction mirrored from test_run_yields_agent_output_when_complete_step_captured
    (same file): _seed_git_and_auth, _canonical_auth_override, FakeCodex
    client_factory, _build_invocation — adapted to add bash to agent_def.tools.
    """
    import anyio as _anyio

    from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
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

    from imp.agents.runner import AgentInvocation, ChangeContext
    from imp.engine.state import InMemoryEventLog

    elog = InMemoryEventLog()
    inv = AgentInvocation(
        agent_def=bash_agent_def,
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="you are a test agent",
        initial_messages=(),
        change_context=ChangeContext(
            slug="s", title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        fs_permissions=(
            FsRule(operations=("read", "write"), paths=("**",),
                   mode="allow", source=_SRC_LOCAL),
        ),
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
            with _anyio.fail_after(10):
                while runner._ctx is None:
                    await _anyio.sleep(0)
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
