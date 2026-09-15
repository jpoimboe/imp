from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.claude import (
    ClaudeAgentRunner,
    _make_impty_sdk_tools,
)
from impty.driver import ImptyDriver
from impty.mcp import THIN_DESCRIPTIONS
from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent(drivers: frozenset[str]) -> AgentDef:
    loc = SourceLoc(file=Path("a.md"), line=1)
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success",), required=True),),
            source=loc),
        prompt_template_source="hi", source_path=Path("a.md"), source=loc,
        drivers=drivers)


def _inv(tmp_path: Path, drivers: frozenset[str]) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent(drivers), provider_name="claude_code",
        model_name="claude-sonnet-4-6", system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(title="t", project_root=tmp_path,
                                     workflow_dir=tmp_path),
        transcript_dir=tmp_path / "step",
events_path=tmp_path / "events.jsonl")


def test_driver_registers_in_process_impty_server_and_wildcard(tmp_path: Path) -> None:
    runner = ClaudeAgentRunner("claude-sonnet-4-6")
    sentinel = object()
    opts = runner._build_options(
        _inv(tmp_path, frozenset({"impty"})),
        mcp_server=object(), tool_names=["bash"], impty_server=sentinel)
    # The in-process impty SDK server is added under the "impty" key, NOT a stdio
    # command/args block.
    assert opts.mcp_servers["impty"] is sentinel
    assert "imp" in opts.mcp_servers
    assert "mcp__impty__*" in opts.allowed_tools


def test_no_driver_no_impty_grant(tmp_path: Path) -> None:
    runner = ClaudeAgentRunner("claude-sonnet-4-6")
    opts = runner._build_options(
        _inv(tmp_path, frozenset()),
        mcp_server=object(), tool_names=["bash"], impty_server=None)
    assert "impty" not in opts.mcp_servers
    assert "mcp__impty__*" not in opts.allowed_tools


@pytest.mark.anyio
async def test_impty_sdk_tools_carry_driver_names_and_descriptions() -> None:
    driver = ImptyDriver()
    sdk_tools = await _make_impty_sdk_tools(driver)
    by_name = {t.name: t for t in sdk_tools}
    assert set(by_name) == {
        "start", "view", "send", "send_signal", "resize",
        "wait", "snapshot", "stop"}
    method_docs = {m.__name__: m.__doc__ for m in driver.tool_methods()}
    for name, sdk in by_name.items():
        # Provider surface carries the thin one-liner, not the full docstring.
        assert sdk.description == THIN_DESCRIPTIONS[name]
        assert sdk.description != method_docs[name]


@pytest.mark.anyio
async def test_run_closes_the_impty_session_in_teardown(
    tmp_path: Path, monkeypatch: Any,
) -> None:
    (tmp_path / "step").mkdir(parents=True, exist_ok=True)
    closed: list[bool] = []
    journal_dirs: list[Path | None] = []

    class _SpyDriver:
        # Mirrors ImptyDriver's real signature: the provider builds it through
        # attach_impty_driver, which passes the run's sandbox hook, this step's
        # transcript dir as the journal dir, and a quest-scoped snapshot dir.
        def __init__(self, sandbox: Any = None, journal_dir: Any = None,
                     snapshot_dir: Any = None, cwd_resolver: Any = None) -> None:
            self._closed = False
            self._sandbox = sandbox
            journal_dirs.append(journal_dir)

        def tool_methods(self) -> list[Any]:
            return []  # no impty tools needed for the teardown assertion

        def close(self) -> None:
            self._closed = True
            closed.append(True)

    monkeypatch.setattr("impty.driver.ImptyDriver", _SpyDriver)

    scripted = [
        assistant(text("done"),
                  tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    runner = ClaudeAgentRunner(
        model_name="claude-sonnet-4-6",
        client_factory=lambda opts: FakeClaudeSDKClient(
            opts, scripted_messages=scripted))
    async for _ in runner.run(_inv(tmp_path, frozenset({"impty"}))):
        pass
    # The per-run driver session is closed in run()'s finally regardless of how
    # the turn ended. TWICE now, and both are wanted: teardown_tool_context ends
    # the session before it kills the namespace the pty lives in, and run()'s own
    # finally still closes the driver. The real ImptyDriver.close() clears only
    # its session, so the second call is a no-op.
    assert closed == [True, True]
    # The journal lands in THIS step's transcript dir, beside its stderr.log.
    assert journal_dirs == [tmp_path / "step"]
