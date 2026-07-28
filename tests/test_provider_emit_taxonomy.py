# tests/test_provider_emit_taxonomy.py
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.paths import project_state_dir
from imp.providers.anthropic import AnthropicAgentRunner
from imp.providers.claude_code import ClaudeCodeAgentRunner
from imp.providers.codex_runner import CodexAgentRunner
from imp.providers.openai import OpenAIAgentRunner
from imp.providers.scripted import ScriptedAgentRunner
from tests.anthropic_fakes import FakeAsyncAnthropic, response, text, tool_use
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
from tests.codex_fakes import (
    FakeCodex,
    agent_message,
    mcp_call,
    thread_started,
    turn_completed,
)
from tests.openai_fakes import FakeResponsesClient, resp_function_call, resp_response


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent_def() -> AgentDef:
    return AgentDef(
        name="t", description="", tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(
                OutputField(name="status", type="enum",
                            values=("Success", "Error"), required=True),
                OutputField(name="note", type="string", required=False),
            ),
            source=SourceLoc(file=Path("a.md"), line=1)),
        prompt_template_source="hi", source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1))


def _inv(tmp_path: Path, log: InMemoryEventLog) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(), provider_name="anthropic",
        model_name="claude-sonnet-4-6", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="T", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log)


@pytest.mark.anyio
async def test_anthropic_fresh_entry_emits_triad_order(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    fake = FakeAsyncAnthropic([
        response(tool_use("c1", "complete_step", {"status": "Success"})),
    ])
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake)
    async for _ in runner.run(_inv(tmp_path, log)):
        pass

    evs = [e for e in log.read() if e.get("agent_id") == "step:p/s"]
    sc = next(e for e in evs if e["type"] == "step_context")
    assert "system_prompt" not in sc["payload"]
    ems = [e for e in evs if e["type"] == "engine_message"]
    assert [e["payload"]["subtype"] for e in ems][:2] == ["system_prompt", "agent_prompt"]
    assert ems[0]["payload"]["content"][0]["text"] == "be terse"
    assert ems[1]["payload"]["content"][0]["text"] == "go"
    assert not [e for e in evs if e["type"] == "user_message"]
    types = [e["type"] for e in evs]
    assert types.index("engine_message") < types.index("agent_message")


@pytest.mark.anyio
async def test_anthropic_handoff_injection_emits_engine_message_continuation(
        tmp_path: Path) -> None:
    # When the runner crosses the context threshold it must inject the handoff
    # instruction as engine_message/continuation, not user_message/human=False.
    log = InMemoryEventLog()
    fake = FakeAsyncAnthropic([
        # Turn 1 reports 170_000 input tokens → crosses 0.8 × 200_000 = 160_000.
        # Uses an undeclared tool so dispatch returns a non-terminal error result
        # and the loop continues to the crossed_now injection site.
        response(tool_use("u1", "bash", {"command": "echo hi", "timeout": 5}),
                 usage={"input_tokens": 170_000}),
        # Turn 2: agent responds to the injected instruction with text and no tool;
        # runner writes the handoff note, emits handoff, and stops.
        response(text("# handoff note"), stop_reason="end_turn"),
    ])
    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="anthropic",
        model_name="claude-sonnet-4-6", system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="T", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/h", event_log=log,
        checkpoint_threshold=0.8)
    runner = AnthropicAgentRunner(
        model_name="claude-sonnet-4-6", client_factory=lambda: fake)

    async for _ in runner.run(inv):
        pass

    evs = [e for e in log.read() if e.get("agent_id") == "step:p/h"]
    continuations = [
        e for e in evs
        if e["type"] == "engine_message" and e["payload"].get("subtype") == "continuation"
    ]
    assert len(continuations) == 1
    assert "approaching the context limit" in continuations[0]["payload"]["content"][0]["text"]
    assert not [e for e in evs if e["type"] == "user_message"]


@pytest.mark.anyio
async def test_openai_fresh_entry_emits_triad_order(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    fake = FakeResponsesClient([
        resp_response(resp_function_call("c1", "complete_step", '{"status":"Success"}')),
    ])
    runner = OpenAIAgentRunner(model_name="gpt-4o-mini", client_factory=lambda: fake)
    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="openai", model_name="gpt-4o-mini",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            id="test", title="T", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log)
    async for _ in runner.run(inv):
        pass
    evs = [e for e in log.read() if e.get("agent_id") == "step:p/s"]
    sc = next(e for e in evs if e["type"] == "step_context")
    assert "system_prompt" not in sc["payload"]
    ems = [e for e in evs if e["type"] == "engine_message"]
    assert [e["payload"]["subtype"] for e in ems][:2] == ["system_prompt", "agent_prompt"]
    assert not [e for e in evs if e["type"] == "user_message"]


def _cc_drive(
    tmp_path: Path, log: InMemoryEventLog,
) -> tuple[ClaudeCodeAgentRunner, AgentInvocation]:
    scripted = [
        cc_assistant(cc_tool_use("c1", "complete_step", {"status": "Success"})),
        cc_result(),
    ]
    def fake_client_factory(opts: Any) -> FakeClaudeSDKClient:
        return FakeClaudeSDKClient(opts, scripted_messages=scripted)
    runner = ClaudeCodeAgentRunner(
        model_name="claude-sonnet-4-5",
        client_factory=fake_client_factory,
    )
    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="claude_code", model_name="claude-sonnet-4-5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="T", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log)
    return runner, inv


@pytest.mark.anyio
async def test_claude_code_fresh_entry_emits_triad_order(tmp_path: Path) -> None:
    (tmp_path / "art").mkdir()
    log = InMemoryEventLog()
    runner, inv = _cc_drive(tmp_path, log)
    async for _ in runner.run(inv):
        pass
    evs = [e for e in log.read() if e.get("agent_id") == "step:p/s"]
    sc = next(e for e in evs if e["type"] == "step_context")
    assert "system_prompt" not in sc["payload"]
    ems = [e for e in evs if e["type"] == "engine_message"]
    assert [e["payload"]["subtype"] for e in ems][:2] == ["system_prompt", "agent_prompt"]
    assert not [e for e in evs if e["type"] == "user_message"]


def _scripted_drive(
    tmp_path: Path, log: InMemoryEventLog, monkeypatch: pytest.MonkeyPatch,
) -> tuple[ScriptedAgentRunner, AgentInvocation]:
    script = {"default": [
        {"text": "done",
         "tools": [{"name": "complete_step",
                    "input": {"outcome": "ok", "status": "Success"}}]},
    ]}
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script_path))
    runner = ScriptedAgentRunner(model_name="x")
    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="scripted", model_name="x",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="T", project_root=tmp_path,
            workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow"),
        transcript_dir=tmp_path / "step", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log)
    return runner, inv


@pytest.mark.anyio
async def test_scripted_fresh_entry_emits_triad_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    log = InMemoryEventLog()
    runner, inv = _scripted_drive(tmp_path, log, monkeypatch)
    async for _ in runner.run(inv):
        pass
    evs = [e for e in log.read() if e.get("agent_id") == "step:p/s"]
    sc = next(e for e in evs if e["type"] == "step_context")
    assert "system_prompt" not in sc["payload"]
    ems = [e for e in evs if e["type"] == "engine_message"]
    assert [e["payload"]["subtype"] for e in ems][:2] == ["system_prompt", "agent_prompt"]
    assert not [e for e in evs if e["type"] == "user_message"]


def _codex_git_init(p: Path) -> None:
    subprocess.run(["git", "init", "-q", str(p)], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.email", "t@t.t"], check=True)
    subprocess.run(["git", "-C", str(p), "config", "user.name", "T"], check=True)
    (p / ".gitkeep").write_text("")
    subprocess.run(["git", "-C", str(p), "add", ".gitkeep"], check=True)
    subprocess.run(["git", "-C", str(p), "commit", "-q", "-m", "init"], check=True)


def _codex_drive(
    tmp_path: Path, log: InMemoryEventLog,
) -> tuple[CodexAgentRunner, AgentInvocation]:
    _codex_git_init(tmp_path)
    (tmp_path / "auth.json").write_text('{"auth_mode": "chatgpt", "tokens": {}}')
    (tmp_path / "tx").mkdir(exist_ok=True)
    scripted = [[
        thread_started("thr-1"),
        agent_message("ok, calling complete_step"),
        mcp_call(name="complete_step", args={"status": "Success"},
                 result="step complete", item_id="cs-1"),
        turn_completed(input_tokens=100, output_tokens=50),
    ]]
    runner = CodexAgentRunner(
        model_name="gpt-5.5",
        client_factory=lambda options=None: FakeCodex(scripted_turns=scripted),
    )
    runner._canonical_auth_override = tmp_path / "auth.json"  # test seam
    inv = AgentInvocation(
        agent_def=_agent_def(), provider_name="codex", model_name="gpt-5.5",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="T", project_root=tmp_path, workflow_dir=tmp_path, id="c1"),
        transcript_dir=tmp_path / "tx", events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s", event_log=log, max_tool_calls=7)
    return runner, inv


@pytest.mark.anyio
async def test_codex_fresh_entry_emits_system_and_agent_prompt(tmp_path: Path) -> None:
    log = InMemoryEventLog()
    runner, inv = _codex_drive(tmp_path, log)
    async for _ in runner.run(inv):
        pass
    evs = [e for e in log.read() if e.get("agent_id") == "step:p/s"]
    sc = [e for e in evs if e["type"] == "step_context"]
    assert len(sc) == 1, evs
    payload = sc[0]["payload"]
    assert payload["provider"] == "codex"
    assert payload["model"] == "gpt-5.5"
    assert payload["max_tool_calls"] == inv.max_tool_calls
    assert "env.CODEX_HOME" in payload["params"]
    ems = [e for e in evs if e["type"] == "engine_message"]
    subs = [e["payload"]["subtype"] for e in ems]
    assert subs[:2] == ["system_prompt", "agent_prompt"]
    assert not [e for e in evs if e["type"] == "user_message"]   # no human spoke
