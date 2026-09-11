from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import pytest

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.resume_state import read_native_resume_state
from imp.engine.types import (
    AgentDef,
    OutputField,
    OutputSchema,
    SourceLoc,
)
from imp.providers.codex_rollout import parse_codex_resume_state
from imp.providers.codex_runner import CodexAgentRunner
from tests.codex_fakes import FakeCodex, thread_started, turn_completed
from tests.conftest import _pid_gone_or_zombie

_SRC = SourceLoc(file=Path("a.md"), line=1)


async def _wait_for_signal(
    predicate: Callable[[], bool], *, runner_done: asyncio.Event | None = None,
) -> None:
    while not predicate():
        if runner_done is not None and runner_done.is_set():
            raise AssertionError("runner finalized before publishing the awaited signal")
        await anyio.sleep(0.02)


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


def _inv(tmp_path: Path) -> AgentInvocation:
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
    )


def _silent_codex(tmp_path: Path) -> Path:
    executable = tmp_path / "codex"
    executable.write_text(f'''#!{sys.executable}
import json
import os
import signal
import subprocess
import sys
from pathlib import Path

codex_home = Path(os.environ["CODEX_HOME"])
rollout = codex_home / "sessions/2026/09/10/rollout-test-thread-hard-stop.jsonl"
rollout.parent.mkdir(parents=True, exist_ok=True)
rollout.write_bytes(b'{{"type":"session_meta"}}\\n')
escapee = subprocess.Popen(
    [sys.executable, "-c", "import signal; signal.pause()"],
    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    start_new_session=True)
Path(os.environ["IMP_TEST_CODEX_PIDS"]).write_text(
    f"{{os.getpid()}} {{escapee.pid}}")
print(json.dumps({{"type": "thread.started", "thread_id": "thread-hard-stop"}}),
      flush=True)
signal.pause()
''')
    executable.chmod(0o755)
    return executable


def _impty_teardown_seam() -> tuple[
    list[int], list[int], list[Path | None], Any, Any,
]:
    shutdown_calls: list[int] = []
    close_calls: list[int] = []
    journal_dirs: list[Path | None] = []

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

    async def fake_start_impty(**kwargs: Any) -> _FakeHandle:
        return _FakeHandle()

    return shutdown_calls, close_calls, journal_dirs, fake_driver, fake_start_impty


@pytest.mark.anyio
async def test_teardown_runs_on_no_complete_step_error_exit(
    tmp_path: Path,
) -> None:
    """The `finally:` in run() must tear down on EVERY exit path, not just
    interrupt(). Here a degenerate top-level agent (no continuation_fn) ends its
    turn without calling complete_step: the shared driver surfaces the
    no_complete_step error (the unified observe/push behavior) and the finally
    must still tear the codex_home down."""
    _seed_auth(tmp_path)
    # A turn that completes WITHOUT calling complete_step. With no continuation_fn
    # wired, the shared driver yields the no_complete_step terminal error.
    scripted = [[thread_started(), turn_completed()]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = tmp_path / "auth.json"  # test seam

    with patch("imp.providers.codex_runner.teardown_codex_home") as td:
        inv = _inv(tmp_path)
        events = [ev async for ev in runner.run(inv)]
        # The degenerate no-complete-step exit is driver-owned: it surfaces the
        # no_complete_step error directly (matching the other providers).
        assert events[-1]["type"] == "error", events
        assert events[-1]["payload"]["kind"] == "no_complete_step"
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
    _seed_auth(tmp_path)
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
    _seed_auth(tmp_path)

    (shutdown_calls, close_calls, journal_dirs, fake_driver,
     fake_start_impty) = _impty_teardown_seam()

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
        quest_context=_inv(tmp_path).quest_context,
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
    )

    with (
        patch("imp.providers.codex_runner.start_impty_server",
              new=fake_start_impty),
        # The provider builds the driver through attach_impty_driver, which passes
        # the run's sandbox hook, this step's transcript dir as the journal dir,
        # and a quest-scoped snapshot dir.
        patch("impty.driver.ImptyDriver",
              new=lambda sandbox=None, journal_dir=None, snapshot_dir=None,
              cwd_resolver=None: journal_dirs.append(journal_dir) or fake_driver),
    ):
        [ev async for ev in runner.run(inv)]

    assert shutdown_calls == [1], "impty_handle.shutdown() must be called in teardown"
    # Twice, and both are wanted: run()'s own finally closes the driver, and
    # teardown_tool_context ends the session before killing the namespace the pty
    # lives in. The real ImptyDriver.close() clears only its session, so whichever
    # lands second is a no-op.
    assert close_calls == [1, 1], "impty_driver.close() must be called in teardown"
    # The journal lands in THIS step's transcript dir, beside its stderr.log.
    assert journal_dirs == [tmp_path / "tx"]


@pytest.mark.anyio
@pytest.mark.timeout(300)
async def test_hard_stop_reaps_real_tree_and_preserves_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_auth(tmp_path)
    _silent_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    pids_path = tmp_path / "codex-pids"
    monkeypatch.setenv("IMP_TEST_CODEX_PIDS", str(pids_path))
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._canonical_auth_override = tmp_path / "auth.json"
    invocation = AgentInvocation(
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
        quest_context=_inv(tmp_path).quest_context,
        transcript_dir=tmp_path / "tx",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:t",
    )
    (shutdown_calls, close_calls, journal_dirs, fake_driver,
     fake_start_impty) = _impty_teardown_seam()
    events: list[dict[str, Any]] = []
    drive_done = asyncio.Event()
    interrupt_done = asyncio.Event()
    pids: list[int] = []
    codex_home: Path | None = None

    async def drive() -> None:
        try:
            async for event in runner.run(invocation):
                events.append(event)
        finally:
            drive_done.set()

    async def interrupt() -> None:
        try:
            await runner.interrupt()
        finally:
            interrupt_done.set()

    waiting_for = "Codex PID publication"
    with (
        patch("imp.providers.codex_runner.start_impty_server",
              new=fake_start_impty),
        patch("impty.driver.ImptyDriver",
              new=lambda sandbox=None, journal_dir=None, snapshot_dir=None,
              cwd_resolver=None: journal_dirs.append(journal_dir) or fake_driver),
    ):
        try:
            with anyio.fail_after(240):
                async with anyio.create_task_group() as task_group:
                    task_group.start_soon(drive)
                    await _wait_for_signal(
                        lambda: (pids_path.exists()
                                 and len(pids_path.read_text().split()) == 2),
                        runner_done=drive_done)
                    pids = [int(pid) for pid in pids_path.read_text().split()]
                    waiting_for = "Codex thread publication"
                    await _wait_for_signal(
                        lambda: runner._proc_state.get("thread_id")
                        == "thread-hard-stop",
                        runner_done=drive_done)
                    waiting_for = "Codex process ownership"
                    await _wait_for_signal(
                        lambda: runner._process_owner.process is not None,
                        runner_done=drive_done)
                    codex_home = runner._codex_home
                    assert codex_home is not None
                    task_group.start_soon(interrupt)
                    waiting_for = "interrupt cancellation"
                    await _wait_for_signal(lambda: runner._process_owner.cancelled)
                    await runner.force_kill()
                    waiting_for = "runner finalization"
                    await _wait_for_signal(lambda: drive_done.is_set())
                    waiting_for = "interrupt acknowledgement"
                    await _wait_for_signal(lambda: interrupt_done.is_set())
                waiting_for = "process-tree reaping"
                await _wait_for_signal(
                    lambda: all(_pid_gone_or_zombie(pid) for pid in pids))
        except TimeoutError:
            pytest.fail(f"timed out waiting for {waiting_for}", pytrace=False)
        finally:
            for pid in pids:
                if not _pid_gone_or_zombie(pid):
                    with contextlib.suppress(ProcessLookupError):
                        os.kill(pid, signal.SIGKILL)

    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["category"] == "config"
    assert events[-1]["payload"]["kind"] == "interrupted"
    assert codex_home is not None and not codex_home.exists()
    assert runner._server_handle.task.done()
    assert runner._process_owner.process is None
    assert shutdown_calls == [1]
    assert close_calls == [1, 1]
    assert journal_dirs == [tmp_path / "tx"]
    state = parse_codex_resume_state(read_native_resume_state(invocation.transcript_dir))
    assert state.thread_id == "thread-hard-stop"
    assert state.restore_path == (
        "sessions/2026/09/10/rollout-test-thread-hard-stop.jsonl")
    assert ((invocation.transcript_dir / state.rollout_slot).read_bytes()
            == b'{"type":"session_meta"}\n')


@pytest.mark.anyio
@pytest.mark.timeout(300)
async def test_prepublication_hard_stop_preserves_runner_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_auth(tmp_path)
    _silent_codex(tmp_path)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    pids_path = tmp_path / "codex-pids"
    monkeypatch.setenv("IMP_TEST_CODEX_PIDS", str(pids_path))
    runner = CodexAgentRunner(model_name="gpt-5.5")
    runner._canonical_auth_override = tmp_path / "auth.json"
    invocation = _inv(tmp_path)
    spawned = asyncio.Event()
    release_publication = asyncio.Event()
    drive_done = asyncio.Event()
    interrupt_done = asyncio.Event()
    events: list[dict[str, Any]] = []
    force_calls: list[None] = []
    pids: list[int] = []
    codex_home: Path | None = None
    real_spawn = asyncio.create_subprocess_exec
    real_force_kill = runner.force_kill

    async def gated_spawn(*args: Any, **kwargs: Any) -> Any:
        process = await real_spawn(*args, **kwargs)
        spawned.set()
        await release_publication.wait()
        return process

    async def recorded_force_kill() -> None:
        force_calls.append(None)
        await real_force_kill()

    async def drive() -> None:
        try:
            async for event in runner.run(invocation):
                events.append(event)
        finally:
            drive_done.set()

    async def interrupt() -> None:
        try:
            await runner.interrupt()
        finally:
            interrupt_done.set()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", gated_spawn)
    runner.force_kill = recorded_force_kill
    waiting_for = "Codex subprocess spawn"
    try:
        with anyio.fail_after(240):
            async with anyio.create_task_group() as task_group:
                task_group.start_soon(drive)
                await spawned.wait()
                waiting_for = "Codex PID publication"
                await _wait_for_signal(
                    lambda: (pids_path.exists()
                             and len(pids_path.read_text().split()) == 2),
                    runner_done=drive_done)
                pids = [int(pid) for pid in pids_path.read_text().split()]
                codex_home = runner._codex_home
                assert codex_home is not None
                assert runner._process_owner.process is None
                task_group.start_soon(interrupt)
                waiting_for = "interrupt cancellation"
                await _wait_for_signal(lambda: runner._process_owner.cancelled)
                await runner.force_kill()
                assert force_calls == [None]
                assert runner._process_owner.process is None
                release_publication.set()
                waiting_for = "runner finalization"
                await _wait_for_signal(lambda: drive_done.is_set())
                waiting_for = "interrupt acknowledgement"
                await _wait_for_signal(lambda: interrupt_done.is_set())
                waiting_for = "process-tree reaping"
                await _wait_for_signal(
                    lambda: all(_pid_gone_or_zombie(pid) for pid in pids))
    except TimeoutError:
        pytest.fail(f"timed out waiting for {waiting_for}", pytrace=False)
    finally:
        release_publication.set()
        for pid in pids:
            if not _pid_gone_or_zombie(pid):
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)

    assert events[-1]["type"] == "error"
    assert events[-1]["payload"]["category"] == "config"
    assert events[-1]["payload"]["kind"] == "interrupted"
    assert codex_home is not None and not codex_home.exists()
    assert runner._server_handle.task.done()
    assert runner._process_owner.process is None
    assert runner._process_owner.acknowledged
    assert force_calls == [None]
