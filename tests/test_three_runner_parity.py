"""Three-runner parity fixture (spec §12 acceptance #2).

Same agent definition. Same canned conversation. Same agent_output.

Each runner gets a fake client wired to emit one assistant turn with a
single complete_step tool_use carrying {"status": "Success", "note": "p"}.
The terminal event is `agent_output`; we assert the payload is identical
across the three.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.agents.tools import get_registry  # noqa: F401
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import AnthropicAgentRunner
from imp.providers.claude_code import ClaudeCodeAgentRunner
from imp.providers.openai import OpenAIAgentRunner
from tests.anthropic_fakes import (
    FakeAsyncAnthropic,
)
from tests.anthropic_fakes import (
    response as a_response,
)
from tests.anthropic_fakes import (
    tool_use as a_tool_use,
)
from tests.cc_fakes import (
    FakeClaudeSDKClient,
)
from tests.cc_fakes import (
    assistant as cc_assistant,
)
from tests.cc_fakes import (
    result as cc_result,
)
from tests.cc_fakes import (
    tool_use as cc_tool_use,
)
from tests.openai_fakes import (
    FakeAsyncOpenAI,
)
from tests.openai_fakes import (
    completion as o_completion,
)
from tests.openai_fakes import (
    message as o_message,
)
from tests.openai_fakes import (
    tool_call as o_tool_call,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _shared_invocation(tmp_path: Path, provider_name: str, model: str) -> AgentInvocation:
    (tmp_path / "art").mkdir(exist_ok=True)
    return AgentInvocation(
        agent_def=AgentDef(
            name="t", description="", tools=frozenset({"complete_step"}),
            subagents=(),
            output_schema=OutputSchema(
                fields=(
                    OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),
                    OutputField(name="note", type="string", required=False),
                ),
                source=SourceLoc(file=Path("a.md"), line=1),
            ),
            prompt_template_source="be terse",
            source_path=Path("a.md"),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        provider_name=provider_name, model_name=model,
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            slug="test", title="Test", project_root=tmp_path,
        ),
        interactive=False,
        transcript_dir=tmp_path / f"step-{provider_name}",
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )


EXPECTED = {"status": "Success", "note": "p"}


async def _drive(runner: Any, inv: AgentInvocation) -> Any:
    final: Any = None
    async for ev in runner.run(inv):
        if ev["type"] == "agent_output":
            final = ev["payload"]
    return final


def _assert_event_log_parity(events_path: Path) -> None:
    """Assert that the event log contains the required audit event kinds:
    step_context + user_message + llm_response (spec §5.3 parity assertion)."""
    events = [json.loads(line) for line in events_path.read_text().splitlines() if line.strip()]
    kinds = {e["type"] for e in events}
    assert "step_context" in kinds, f"missing step_context in {kinds}"
    assert "user_message" in kinds, f"missing user_message in {kinds}"
    assert "llm_response" in kinds, f"missing llm_response in {kinds}"


@pytest.mark.anyio
async def test_anthropic_runner_matches(tmp_path: Path) -> None:
    fake = FakeAsyncAnthropic([
        a_response(
            a_tool_use("c1", "complete_step", EXPECTED),
            stop_reason="tool_use",
        ),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    inv = _shared_invocation(tmp_path, "anthropic", "claude-sonnet-4-6")
    out = await _drive(runner, inv)
    assert out == EXPECTED
    _assert_event_log_parity(inv.events_path)


@pytest.mark.anyio
async def test_openai_runner_matches(tmp_path: Path) -> None:
    fake = FakeAsyncOpenAI([
        o_completion(o_message(
            content=None,
            tool_calls=[o_tool_call("c1", "complete_step", EXPECTED)],
        )),
    ])
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: fake,
    )
    inv = _shared_invocation(tmp_path, "openai", "gpt-4o-mini")
    out = await _drive(runner, inv)
    assert out == EXPECTED
    _assert_event_log_parity(inv.events_path)


@pytest.mark.anyio
async def test_claude_code_runner_matches(tmp_path: Path) -> None:
    scripted = [
        cc_assistant(
            cc_tool_use("c1", "complete_step", EXPECTED),
        ),
        cc_result(),
    ]
    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    inv = _shared_invocation(tmp_path, "claude_code", "claude-sonnet-4-5")
    out = await _drive(runner, inv)
    assert out == EXPECTED
    _assert_event_log_parity(inv.events_path)
