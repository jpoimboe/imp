"""Cross-vendor parity: load_skill dispatches and emits correctly on all three providers.

Modeled on tests/test_three_runner_parity.py.  For each provider we drive a
single step whose scripted assistant response calls load_skill(name="refactor").

For Anthropic and OpenAI the tool is dispatched IN-PROCESS: the runner's
dispatch loop calls tool.async_invoke(), which reads skills/refactor.md and
emits a skill_load event.  The tool_result content in the event log is the
real skill body returned by the tool.

For ClaudeCode the SDK/MCP path handles dispatch; the FakeClaudeSDKClient
does NOT invoke tools in-process.  We supply a scripted UserMessage carrying
the tool_result so the transcript is balanced and the runner logs the
tool_result event.  Because the tool body never runs in the fake, the
skill_load event is NOT emitted for this provider — we assert only the
in-process providers (anthropic, openai) for assertion 2.

Assertion layout
----------------
1. tool_result content == SKILL_BODY
      Anthropic/OpenAI: real in-process invocation → real body
      ClaudeCode:       scripted FakeToolResultBlock content → same body

2. Exactly ONE skill_load event with payload={"name":"refactor","source":"agent"}
   and agent_id is None.
      Anthropic/OpenAI ONLY (ClaudeCode fake cannot emit this — see above).

3. load_skill present in step_context tools_schema even though the AgentDef
   did NOT declare it (auto-injected by _build_tools in all three providers).
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
    tool_result as cc_tool_result,
)
from tests.cc_fakes import (
    tool_use as cc_tool_use,
)
from tests.cc_fakes import (
    user as cc_user,
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


SKILL_BODY = "# Refactor\ndo the thing\n"
COMPLETE_STEP_ARGS = {"status": "Success", "note": "done"}


def _make_invocation(tmp_path: Path, provider_name: str, model: str) -> AgentInvocation:
    """Build a shared AgentInvocation with workflow_dir containing skills/refactor.md.

    The AgentDef intentionally does NOT declare load_skill in its tools set —
    assertion 3 verifies it still appears in the step_context tools_schema.
    """
    workflow_dir = tmp_path / ".imp" / "change" / "workflow"
    skills_dir = workflow_dir / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (skills_dir / "refactor.md").write_text(SKILL_BODY)

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
            workflow_dir=workflow_dir,
        ),
        interactive=False,
        transcript_dir=tmp_path / f"step-{provider_name}",
        fs_permissions=(FsRule(
            operations=("read",), paths=("/project/**",), mode="allow",
            source=SourceLoc(file=Path("w.yaml"), line=1),
        ),),
        events_path=tmp_path / "events.jsonl",
    )


async def _drain(runner: Any, inv: AgentInvocation) -> Any:
    """Run the runner to completion and return the agent_output payload."""
    final: Any = None
    async for ev in runner.run(inv):
        if ev["type"] == "agent_output":
            final = ev["payload"]
    return final


def _read_events(events_path: Path) -> list[dict[str, Any]]:
    return [
        json.loads(line)
        for line in events_path.read_text().splitlines()
        if line.strip()
    ]


def _assert_tool_result_content(events: list[dict[str, Any]], tool_id: str) -> None:
    """Assertion 1: tool_result event for tool_id has content == SKILL_BODY."""
    results = [
        e for e in events
        if e["type"] == "tool_result" and e["payload"]["id"] == tool_id
    ]
    assert len(results) == 1, (
        f"expected exactly 1 tool_result for id={tool_id!r}, "
        f"found {len(results)}: {results}"
    )
    assert results[0]["payload"]["content"] == SKILL_BODY, (
        f"tool_result content mismatch: {results[0]['payload']['content']!r}"
    )


def _assert_skill_load_event(events: list[dict[str, Any]]) -> None:
    """Assertion 2: exactly ONE skill_load event with correct payload/agent_id."""
    loads = [e for e in events if e["type"] == "skill_load"]
    assert len(loads) == 1, f"expected 1 skill_load event, got {len(loads)}: {loads}"
    assert loads[0]["payload"] == {"name": "refactor", "source": "agent"}, (
        f"skill_load payload mismatch: {loads[0]['payload']}"
    )
    assert loads[0]["agent_id"] is None, (
        f"skill_load must have agent_id=None, got {loads[0]['agent_id']!r}"
    )


def _assert_load_skill_in_schema(events: list[dict[str, Any]]) -> None:
    """Assertion 3: load_skill appears in the step_context tools_schema."""
    ctx_events = [e for e in events if e["type"] == "step_context"]
    assert ctx_events, "no step_context event found"
    schema = ctx_events[0]["payload"]["tools_schema"]
    names = {
        (t.get("name") or t.get("function", {}).get("name", ""))
        for t in schema
    }
    assert "load_skill" in names, (
        f"load_skill not found in tools_schema names {names}"
    )


# ── Anthropic ─────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_anthropic_load_skill_parity(tmp_path: Path) -> None:
    """Anthropic: load_skill dispatches in-process, emits skill_load event."""
    fake = FakeAsyncAnthropic([
        # Turn 1: LLM calls load_skill — tool dispatched in-process
        a_response(
            a_tool_use("tu1", "load_skill", {"name": "refactor"}),
            stop_reason="tool_use",
        ),
        # Turn 2: LLM calls complete_step — terminates the step
        a_response(
            a_tool_use("tu2", "complete_step", COMPLETE_STEP_ARGS),
            stop_reason="tool_use",
        ),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake,
    )
    inv = _make_invocation(tmp_path, "anthropic", "claude-sonnet-4-6")
    out = await _drain(runner, inv)
    assert out == COMPLETE_STEP_ARGS

    events = _read_events(inv.events_path)
    _assert_tool_result_content(events, "tu1")   # assertion 1
    _assert_skill_load_event(events)              # assertion 2
    _assert_load_skill_in_schema(events)          # assertion 3


# ── OpenAI ────────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_openai_load_skill_parity(tmp_path: Path) -> None:
    """OpenAI: load_skill dispatches in-process, emits skill_load event."""
    fake = FakeAsyncOpenAI([
        # Turn 1: LLM calls load_skill — tool dispatched in-process
        o_completion(o_message(
            content=None,
            tool_calls=[o_tool_call("tu1", "load_skill", {"name": "refactor"})],
        )),
        # Turn 2: LLM calls complete_step — terminates the step
        o_completion(o_message(
            content=None,
            tool_calls=[o_tool_call("tu2", "complete_step", COMPLETE_STEP_ARGS)],
        )),
    ])
    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini", client_factory=lambda: fake,
    )
    inv = _make_invocation(tmp_path, "openai", "gpt-4o-mini")
    out = await _drain(runner, inv)
    assert out == COMPLETE_STEP_ARGS

    events = _read_events(inv.events_path)
    _assert_tool_result_content(events, "tu1")   # assertion 1
    _assert_skill_load_event(events)              # assertion 2
    _assert_load_skill_in_schema(events)          # assertion 3


# ── ClaudeCode ────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_claude_code_load_skill_parity(tmp_path: Path) -> None:
    """ClaudeCode: tool_result logged from scripted result; schema includes load_skill.

    NOTE: The FakeClaudeSDKClient does not invoke tools in-process (the real
    SDK/MCP dispatch path is bypassed by the fake).  Assertion 2 (skill_load
    event) is therefore NOT asserted here — it is only emitted when the tool
    runs in-process (Anthropic and OpenAI providers).  The other two assertions
    remain meaningful:
      - Assertion 1: the scripted tool_result content is logged in the event log
      - Assertion 3: load_skill appears in step_context tools_schema
    """
    scripted = [
        # Turn 1: LLM calls load_skill
        cc_assistant(
            cc_tool_use("tu1", "load_skill", {"name": "refactor"}),
        ),
        # MCP server returns the skill body (scripted, not real dispatch)
        cc_user(
            cc_tool_result("tu1", SKILL_BODY),
        ),
        # LLM calls complete_step to finish
        cc_assistant(
            cc_tool_use("tu2", "complete_step", COMPLETE_STEP_ARGS),
        ),
        cc_result(),
    ]

    def factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)

    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5", client_factory=factory,
    )
    inv = _make_invocation(tmp_path, "claude_code", "claude-sonnet-4-5")
    out = await _drain(runner, inv)
    assert out == COMPLETE_STEP_ARGS

    events = _read_events(inv.events_path)
    _assert_tool_result_content(events, "tu1")   # assertion 1 (scripted result)
    # Assertion 2 (skill_load) is skipped: FakeClaudeSDKClient does not
    # dispatch tools in-process, so the skill body is never read and
    # skill_load is never emitted.  This is a known limitation of the fake
    # harness, not a production bug.
    _assert_load_skill_in_schema(events)          # assertion 3
