# tests/test_claude_code_stderr_capture.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("claude_agent_sdk")  # opts (incl. stderr=) only build with the real SDK

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow",
        ),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=InMemoryEventLog(),
    )


class _StderrThenDieOnConnect:
    """Fake SDK client: simulate the CLI writing stderr, then fail on connect
    (the startup ProcessError that yields no ResultMessage)."""

    def __init__(self, opts: Any) -> None:
        self._opts = opts

    async def __aenter__(self) -> _StderrThenDieOnConnect:
        self._opts.stderr("claude: fatal: simulated startup boom\n")
        raise RuntimeError("Command failed with exit code 1")

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


@pytest.mark.anyio
async def test_startup_failure_carries_stderr_tail_and_writes_file(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: _StderrThenDieOnConnect(opts),
    )
    inv = _inv(tmp_path)

    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert len(errs) == 1
    payload = errs[0]["payload"]
    assert payload["category"] == "unknown"
    assert "simulated startup boom" in str(payload["detail"].get("stderr_tail"))

    # The full stream also landed on disk in the step dir.
    log = tmp_path / "step" / "stderr.log"
    assert log.exists()
    assert "simulated startup boom" in log.read_text()


@pytest.mark.anyio
async def test_build_options_registers_stderr_callback(tmp_path: Path) -> None:
    from imp.providers.stderr_sink import StderrSink

    runner = ClaudeCodeAgentRunner(model_name="claude-sonnet-4-5")
    runner._stderr_sink = StderrSink(tmp_path / "step" / "stderr.log")
    from claude_agent_sdk import create_sdk_mcp_server

    server = create_sdk_mcp_server(name="imp", tools=[])
    opts = runner._build_options(_inv(tmp_path), server, ["complete_step"])
    # Bound methods aren't identity-equal across accesses (`a.feed is a.feed`
    # is False in CPython), so compare with == and confirm it feeds the sink.
    assert opts.stderr == runner._stderr_sink.feed
    opts.stderr("probe\n")
    assert runner._stderr_sink.tail() == "probe\n"


class _StderrWarnThenDie:
    """Fake SDK client: write a startup Warning to stderr, then fail on connect
    (the ProcessError path). The finally closes the sink, triggering the bridge."""

    def __init__(self, opts: Any) -> None:
        self._opts = opts

    async def __aenter__(self) -> _StderrWarnThenDie:
        self._opts.stderr(
            "claude: Warning: Custom betas are only available for API key users. "
            "Ignoring provided betas.\n")
        raise RuntimeError("Command failed with exit code 1")

    async def __aexit__(self, *_exc: Any) -> bool:
        return False


@pytest.mark.anyio
async def test_claude_bridges_stderr_warning_into_runner_warning(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=lambda opts: _StderrWarnThenDie(opts),
    )
    inv = _inv(tmp_path)  # event_log is an InMemoryEventLog on the invocation

    _events = [ev async for ev in runner.run(inv)]

    warns = [e for e in inv.event_log.read() if e["type"] == "runner_warning"]
    assert len(warns) == 1
    assert warns[0]["payload"]["message"] == (
        "claude stderr: Warning: Custom betas are only available for API key "
        "users. Ignoring provided betas.")
