"""Background-execution parity for the MCP-wrapped providers.

claude_code and codex execute imp's registry tools in-process (SDK MCP
wrappers / codex_mcp.dispatch_tool) — the spec's "bash never fires there"
claim was false at the production site. This file pins the parity wiring:
injection, the background_enabled flag, and teardown surfaces."""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path

import anyio
import pytest

from imp.providers import claude_code as cc_mod
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.test_bg_spawn import _cleanup
from tests.test_build_tools_wait_for_user import _Inv


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def test_cc_build_tools_injects_bg_with_bash() -> None:
    tools = cc_mod._build_tools(_Inv(["complete_step", "bash"]))
    assert "bash_output" in tools and "bash_kill" in tools


def test_cc_build_tools_no_bg_without_bash() -> None:
    tools = cc_mod._build_tools(_Inv(["complete_step", "read_file"]))
    assert "bash_output" not in tools and "bash_kill" not in tools


def test_codex_reuses_cc_build_tools() -> None:
    """codex_runner imports _build_tools from claude_code (codex_runner.py:222)
    — pin the reuse so the injection above covers codex too. If this ever
    fails, codex grew its own builder and needs its own injection + tests."""
    import inspect

    from imp.providers import codex_runner
    src = inspect.getsource(codex_runner)
    assert "from imp.providers.claude_code import _build_tools" in src


# ---------------------------------------------------------------------------
# Helpers for teardown tests
# ---------------------------------------------------------------------------

def _bg_ctx(tmp_path: Path):
    """A ToolContext shaped like the CC runner's, with bg enabled."""
    from imp.agents.runner import ChangeContext
    from imp.agents.tools.context import ToolContext
    run_dir = tmp_path / ".imp" / "change"
    run_dir.mkdir(parents=True, exist_ok=True)
    ctx = ToolContext(project_root=tmp_path,
                      events_path=run_dir / "events.jsonl",
                      agent_id="step:t/coder", background_enabled=True)
    ctx.change_context = ChangeContext(
        title="T", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    return ctx


async def _spawn_bg(ctx) -> dict:
    from imp.agents.tools import get_registry
    raw = await get_registry()["bash"].async_invoke(
        ctx, {"command": "sleep 60", "run_in_background": True})
    return json.loads(raw)


# ---------------------------------------------------------------------------
# Teardown unit tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cc_cooperative_interrupt_tears_down_background(tmp_path: Path) -> None:
    """A cooperative interrupt tears down the bg group and persists the interrupted
    tombstone — via the steer-landing sweep in run()'s receive loop (interrupt() is
    cooperative-only; it no longer sweeps subprocesses directly)."""
    import asyncio
    from typing import Any

    from imp.cli.signals import InterruptCoordinator
    from tests.cc_fakes import CooperativeClient, assistant, result, text
    from tests.test_claude_code_runner import _inv

    coord = InterruptCoordinator()
    after_steer = [assistant(text("done")), result()]
    holder: dict[str, CooperativeClient] = {}

    def factory(opts: Any) -> CooperativeClient:
        c = CooperativeClient(opts, after_steer=after_steer)
        holder["c"] = c
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
        user_input_fn=lambda _prompt: "carry on")
    coord.attach_runner(runner.interrupt)
    inv = _inv(tmp_path, tools=frozenset({"complete_step", "bash"}))

    async def drive() -> None:
        async for _ev in runner.run(inv):
            pass

    task = asyncio.create_task(drive())
    try:
        for _ in range(1000):
            if "c" in holder and runner._ctx is not None:
                break
            await asyncio.sleep(0)
        await asyncio.sleep(0.02)
        out = await _spawn_bg(runner._ctx)

        await coord.signal_pause()
        await asyncio.wait_for(task, timeout=3)

        with pytest.raises(ProcessLookupError):
            os.killpg(out["pid"], 0)
        bp = runner._ctx.background_processes[out["id"]]
        assert bp.status == "killed" and bp.kill_reason == "interrupted"
    finally:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if runner._ctx is not None:
            await _cleanup(runner._ctx)


def test_cc_interrupt_none_ctx_safe(tmp_path: Path) -> None:
    async def main() -> None:
        runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
        await runner.interrupt()            # _ctx is None — must not raise
    anyio.run(main)


# ---------------------------------------------------------------------------
# run()-finally e2e teardown test
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_cc_gen_close_runs_teardown(tmp_path: Path) -> None:
    """Teardown site 2 under claude_code: aclosing run() mid-conversation
    kills the backgrounded group via the new finally (PEP 525: _run_loop's
    own finally would only GC-finalize).

    HARNESS ADAPTATION (DONE_WITH_CONCERNS): FakeClaudeSDKClient does not
    dispatch MCP tools — bash tool_use blocks in scripted messages are
    observed by the runner but NOT executed (the real SDK dispatches tools
    asynchronously via MCP transport). Therefore:
      (a) A custom stalling client is used whose query() blocks indefinitely
          (anyio.sleep_forever), preventing the runner from progressing.
      (b) The generator is driven in a task group; the main task polls until
          _ctx is set (which happens synchronously before the first query()),
          then directly spawns a bg process via _spawn_bg on runner._ctx.
      (c) The task group is cancelled, which propagates CancelledError through
          run()'s async-for, firing the try/finally teardown block.
    This correctly pins "aclose → bg group dead via run()'s finally" without
    depending on MCP tool dispatch through the fake harness.
    """
    from tests.test_claude_code_runner import _inv

    class _StallingClient:
        """Client whose query() stalls forever, allowing mid-drive ctx inspection."""
        async def __aenter__(self) -> _StallingClient:
            return self

        async def __aexit__(self, *_: object) -> bool:
            return False

        async def query(self, prompt: object) -> None:
            await anyio.sleep_forever()  # stall until cancelled

        async def receive_response(self):  # type: ignore[override]
            # Never reached: query() stalls before receive_response() is called
            yield  # pragma: no cover

        async def interrupt(self) -> None:
            pass

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda _opts: _StallingClient(),
    )
    inv = _inv(tmp_path, tools=frozenset({"complete_step", "bash"}))
    gen = runner.run(inv)

    bg_pid: int | None = None
    bg_id: str | None = None

    try:
        async with anyio.create_task_group() as tg:
            async def _drive() -> None:
                async for _ev in gen:
                    pass  # pragma: no cover

            tg.start_soon(_drive)
            # _ctx is set synchronously before the first client.query() call;
            # yield control once so _run_loop can advance to that point.
            with anyio.fail_after(5):
                while runner._ctx is None:
                    await anyio.sleep(0)
            out = await _spawn_bg(runner._ctx)
            bg_pid = out["pid"]
            bg_id = out["id"]
            # Cancel the drive task — propagates through run()'s finally
            tg.cancel_scope.cancel()

        # run()'s finally must have called terminate_background_processes
        assert bg_pid is not None
        with pytest.raises(ProcessLookupError):
            os.killpg(bg_pid, 0)
        assert bg_id is not None
        bp = runner._ctx.background_processes[bg_id]
        assert bp.status == "killed" and bp.kill_reason == "interrupted"
    finally:
        if runner._ctx is not None:
            await _cleanup(runner._ctx)


# ---------------------------------------------------------------------------
# codex teardown unit test
# ---------------------------------------------------------------------------

def test_codex_interrupt_tears_down_background(tmp_path: Path) -> None:
    """Decision 7 site 1 under codex: interrupt() kills bg groups and persists
    tombstones. Unit-level: _ctx is set directly (the runner sets it the same
    way mid-_run_loop after preflight).

    CodexAgentRunner construction adaptation: CodexAgentRunner.__init__ only
    requires `model_name` (plus optional kwargs client_factory/user_input_fn);
    no additional required kwargs vs ClaudeCodeAgentRunner. The _ctx is
    injected directly as in the CC sibling — no preflight/server lifecycle
    needed for this unit test.
    """
    async def main() -> None:
        from imp.providers.codex_runner import CodexAgentRunner
        runner = CodexAgentRunner(model_name="gpt-5.5")
        ctx = _bg_ctx(tmp_path)
        runner._ctx = ctx
        out = await _spawn_bg(ctx)
        try:
            await runner.interrupt()
            with pytest.raises(ProcessLookupError):
                os.killpg(out["pid"], 0)
            bp = ctx.background_processes[out["id"]]
            assert bp.status == "killed" and bp.kill_reason == "interrupted"
        finally:
            await _cleanup(ctx)
    anyio.run(main)
