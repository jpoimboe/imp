from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.types import (
    AgentDef,
    OutputField,
    OutputSchema,
    SourceLoc,
)
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex, thread_started, turn_completed

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


def _inv(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="you are a test agent",
        initial_messages=(),
        change_context=ChangeContext(
            title="t", project_root=tmp_path,
            workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
    )


@pytest.mark.anyio
async def test_teardown_runs_on_no_complete_step_error_exit(
    tmp_path: Path,
) -> None:
    """The `finally:` in run() must tear down on EVERY exit path, not just
    interrupt(). Here a degenerate top-level agent (no continuation_fn) ends its
    turn without calling complete_step: run() returns silently — drive_invocation
    surfaces the errored (no agent_output) result downstream — but the finally
    must still tear the codex_home down."""
    _seed_git_and_auth(tmp_path)
    # A turn that completes WITHOUT calling complete_step. With no continuation_fn
    # wired, the degenerate top-level path returns without yielding any event.
    scripted = [[thread_started(), turn_completed()]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = tmp_path / "auth.json"  # test seam

    with patch("imp.providers.codex_runner.teardown_codex_home") as td:
        inv = _inv(tmp_path)
        events = [ev async for ev in runner.run(inv)]
        # Sanity: the degenerate no-complete-step exit yields nothing from run();
        # the error is surfaced downstream by drive_invocation, not here.
        assert events == [], events
        assert td.called
        kwargs = td.call_args.kwargs
        # Each codex_home is unique per run.
        assert "codex_home" in kwargs
        assert kwargs["mode"] == "chatgpt"


@pytest.mark.anyio
async def test_server_shutdown_runs_when_provisioning_fails(
    tmp_path: Path,
) -> None:
    """Leak-fix (Task 31): if `provision_codex_home` raises AFTER the MCP
    server has started, the widened try/finally must still shut the server
    down. Since no codex_home was created, `teardown_codex_home` must NOT be
    called.

    Pre-fix, `provision_codex_home` sat OUTSIDE any try/finally, so a raise
    there leaked the running server (and any partial CODEX_HOME). This test
    targets `server_handle.shutdown()`, which only runs for a provision failure
    once the try is widened to cover provision.
    """
    _seed_git_and_auth(tmp_path)
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=[[]]),
    )
    runner._canonical_auth_override = tmp_path / "auth.json"
    inv = _inv(tmp_path)

    shutdown_calls: list[int] = []
    # Import from the defining module (codex_runner only re-imports it); the
    # patch target below still points at the codex_runner-bound name.
    from imp.providers.codex_mcp import start_server as real_start_server

    async def _tracked_start_server(**kwargs: Any) -> Any:
        handle = await real_start_server(**kwargs)
        orig_shutdown = handle.shutdown

        async def _shutdown() -> None:
            shutdown_calls.append(1)
            await orig_shutdown()

        # ServerHandle is a frozen-by-default? It's a plain dataclass; rebind
        # the bound method via object.__setattr__ to be safe.
        object.__setattr__(handle, "shutdown", _shutdown)
        return handle

    def _boom(**_kwargs: Any) -> Path:
        raise RuntimeError("provision blew up (synthetic)")

    with (
        patch("imp.providers.codex_runner.start_server",
              new=_tracked_start_server),
        patch("imp.providers.codex_runner.provision_codex_home", new=_boom),
        patch("imp.providers.codex_runner.teardown_codex_home") as td,
    ):
        events = [ev async for ev in runner.run(inv)]

    # The run surfaced an error event (provision failure → unknown error).
    assert events, "expected at least one yielded event"
    assert events[-1]["type"] == "error", events
    # The server was shut down despite the provision failure.
    assert shutdown_calls == [1], (
        "MCP server was not shut down on provision failure (leak)")
    # No codex_home was created, so teardown_codex_home must be skipped.
    assert not td.called, (
        "teardown_codex_home must not run when codex_home was never created")


@pytest.mark.anyio
async def test_codex_teardown_closes_impty_session(
    tmp_path: Path,
) -> None:
    """Codex runner's finally block must call impty_handle.shutdown() and
    impty_driver.close() when impty is in the agent's drivers."""
    _seed_git_and_auth(tmp_path)

    shutdown_calls: list[int] = []
    close_calls: list[int] = []

    class _FakeHandle:
        url = "http://127.0.0.1:19999"
        host = "127.0.0.1"
        port = 19999

        async def shutdown(self) -> None:
            shutdown_calls.append(1)

    class _FakeDriver:
        def close(self) -> None:
            close_calls.append(1)

    fake_driver = _FakeDriver()

    async def _fake_start_impty(**kwargs: Any) -> _FakeHandle:
        return _FakeHandle()

    scripted = [[thread_started(), turn_completed()]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = tmp_path / "auth.json"

    inv = AgentInvocation(
        agent_def=AgentDef(
            name="t",
            description="",
            tools=frozenset({"complete_step"}),
            subagents=(),
            output_schema=_agent_def().output_schema,
            prompt_template_source="",
            source_path=Path("a.md"),
            source=_SRC,
            tier=None,
            drivers=frozenset({"impty"}),
        ),
        provider_name="codex",
        model_name="gpt-5.5",
        system_prompt="you are a test agent",
        initial_messages=(),
        change_context=_inv(tmp_path).change_context,
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
    )

    with (
        patch("imp.providers.codex_runner.start_impty_server",
              new=_fake_start_impty),
        patch("impty.driver.ImptyDriver",
              new=lambda: fake_driver),
    ):
        [ev async for ev in runner.run(inv)]

    assert shutdown_calls == [1], "impty_handle.shutdown() must be called in teardown"
    assert close_calls == [1], "impty_driver.close() must be called in teardown"
