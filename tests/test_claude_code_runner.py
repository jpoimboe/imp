# tests/test_claude_code_runner.py
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner
from tests.cc_fakes import (
    FakeClaudeSDKClient,
    assistant,
    result,
    text,
    thinking,
    tool_use,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def(tools: frozenset[str] = frozenset({"complete_step"})) -> AgentDef:
    return AgentDef(
        name="t", description="", tools=tools, subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
                OutputField(name="note", type="string", required=False),
            ),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path, tools: frozenset[str] | None = None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(tools or frozenset({"complete_step"})),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            change_id=1, project_root=tmp_path, artifacts_dir=tmp_path / "art",
        ),
        interactive=False, transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )


@pytest.mark.anyio
async def test_runs_to_complete_step(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()

    scripted = [
        assistant(
            text("calling complete_step"),
            tool_use("c1", "complete_step", {"status": "Success", "note": "ok"}),
        ),
        # Claude's auto-generated user message carrying the tool_result for c1
        # would normally come back from the MCP server. The fake stops here;
        # the runner intercepts complete_step via state, not via tool_result
        # observation.
        result(),
    ]

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=fake_client_factory,
    )
    events = []
    async for ev in runner.run(_inv(tmp_path)):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert "agent_message" in kinds
    assert kinds[-1] == "agent_output"
    assert events[-1]["payload"] == {"status": "Success", "note": "ok"}


@pytest.mark.anyio
async def test_resume_from_existing_transcript(tmp_path: Path) -> None:
    """Pre-populate transcript with a user/assistant/user(tool_result) sequence;
    runner skips initial_messages and replays history via client.query."""
    from imp.agents.runner import ToolResultBlock, ToolUseBlock
    from imp.agents.transcript import Transcript

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(
            TextBlock(text="thinking…"),
            ToolUseBlock(id="t0", name="read_file", input={"path": "/p/x"}),
        ),
    ))
    t.append(Message(
        role="user",
        content=(ToolResultBlock(tool_use_id="t0", content="x"),),
    ))

    scripted = [
        assistant(
            text("done"),
            tool_use("c1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        c = FakeClaudeSDKClient(opts, scripted_messages=scripted)
        return c

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    assert events[-1]["type"] == "agent_output"
    # Initial messages were NOT re-appended.
    loaded = t.load()
    assert len(loaded) == 4  # 3 original + 1 new assistant turn


@pytest.mark.anyio
async def test_runs_subagent_via_task(tmp_path: Path) -> None:
    """Parent dispatches via task → child runner runs to complete_step →
    parent receives JSON-stringified payload and emits its own complete_step."""
    from dataclasses import replace

    (tmp_path / "art").mkdir()

    sub_def = AgentDef(
        name="sub", description="", tools=frozenset({"complete_step"}),
        subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(
                name="status", type="enum",
                values=("Success", "Error"), required=True,
            ),),
            source=SourceLoc(file=Path("sub.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("sub.md"),
        source=SourceLoc(file=Path("sub.md"), line=1),
    )

    parent_def = _agent_def(tools=frozenset({"complete_step", "task"}))
    inv = _inv(tmp_path)
    inv = replace(
        inv,
        agent_def=parent_def,
        subagent_defs={"sub": sub_def},
    )

    parent_scripted = [
        assistant(
            text("dispatching"),
            tool_use("p1", "task",
                     {"subagent_name": "sub", "task_description": "do work"}),
        ),
        # SDK will send a tool_result back from the MCP server's task wrapper.
        # The fake doesn't simulate that round trip; the runner sees the
        # parent's next agent turn directly.
        assistant(
            text("got result"),
            tool_use("p2", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]
    child_scripted = [
        assistant(
            text("sub working"),
            tool_use("s1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    scripts = iter([parent_scripted, child_scripted])

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=next(scripts))

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    assert events[-1]["type"] == "agent_output"
    assert events[-1]["payload"] == {"status": "Success"}


@pytest.mark.anyio
async def test_interactive_resume_from_prompt_user_prompts_then_continues(
    tmp_path: Path,
) -> None:
    from dataclasses import replace

    from imp.agents.runner import Message, TextBlock
    from imp.agents.transcript import Transcript
    from tests.cc_fakes import FakeClaudeSDKClient, assistant, result, text, tool_use

    (tmp_path / "art").mkdir()
    inv = _inv(tmp_path)
    inv = replace(inv, interactive=True)
    inv.transcript_dir.mkdir(parents=True, exist_ok=True)

    t = Transcript(inv.transcript_dir / "transcript.jsonl")
    t.append(Message(role="user", content=(TextBlock(text="go"),)))
    t.append(Message(
        role="assistant",
        content=(TextBlock(text="confirm?"),),
    ))

    scripted = [
        assistant(text("done"), tool_use("c1", "complete_step", {"status": "Success"})),
        result(),
    ]
    user_inputs = iter(["yes, proceed"])
    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=factory,
        user_input_fn=lambda _prompt: next(user_inputs),
    )

    events = []
    async for ev in runner.run(inv):
        events.append(ev)

    kinds = [e["type"] for e in events]
    user_idx = kinds.index("user_input")
    msg_idx = kinds.index("agent_message")
    assert user_idx < msg_idx
    assert kinds[-1] == "agent_output"
    loaded = t.load()
    assert len(loaded) >= 3
    assert loaded[2].role == "user"
    assert isinstance(loaded[2].content[0], TextBlock)
    assert loaded[2].content[0].text == "yes, proceed"


@pytest.mark.anyio
async def test_thinking_block_emits_thinking_event(tmp_path: Path) -> None:
    """Extended-thinking output from claude_code is captured as a `thinking`
    event with the full content, not folded into runner_warning."""
    (tmp_path / "art").mkdir()
    scripted = [
        assistant(
            thinking("weighing options..."),
            tool_use("c1", "complete_step", {"status": "Success"}),
        ),
        result(),
    ]

    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=fake_client_factory,
    )
    events = [ev async for ev in runner.run(_inv(tmp_path))]

    kinds = [e["type"] for e in events]
    assert "thinking" in kinds
    thinkings = [e["payload"]["content"] for e in events if e["type"] == "thinking"]
    assert thinkings == ["weighing options..."]
    assert kinds[-1] == "agent_output"


def test_claude_code_provider_registered_and_resolves() -> None:
    """The claude_code provider is registered eagerly; resolve_role picks it up
    when claude is on PATH."""
    import importlib
    import shutil

    import imp.providers
    from imp.providers import resolve
    from imp.providers.claude_code import ClaudeCodeProvider
    from imp.providers.config import ProviderModel, RoleConfig

    # importlib.reload re-runs the package's top-level register_provider
    # calls. import_module would be a no-op since the module is already
    # cached in sys.modules.
    resolve.reset_providers_for_tests()
    importlib.reload(imp.providers)

    role = RoleConfig(
        name="r", prefer=(ProviderModel(provider="claude_code", model="claude-sonnet-4-5"),),
    )
    if shutil.which("claude") is None:
        # Not on PATH — resolve should fail with ProviderUnavailable.
        with pytest.raises(resolve.ProviderUnavailable):
            resolve.resolve_role(role)
        return

    provider = resolve.resolve_role(role)
    assert provider.name == "claude_code"
    assert isinstance(provider, ClaudeCodeProvider)
    assert provider.model == "claude-sonnet-4-5"
