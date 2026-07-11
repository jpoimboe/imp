from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.agents.subagents import dispatch_task, make_task_tool
from imp.agents.tools.context import ToolContext
from imp.cli.signals import InterruptCoordinator
from imp.engine.agent_run import (
    SUBAGENT_DELIVERABLE_NOTE,
    build_agent_invocation,
    drive_invocation,
    last_assistant_text,
)
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.scripted import ScriptedAgentRunner, _build_tools


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def _agent_def(name: str = "sub") -> AgentDef:
    return AgentDef(
        name=name, description="",
        tools=frozenset({"complete_step", "read_file"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="do the thing",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _sub_invocation(tmp_path: Path, *, event_log=None) -> AgentInvocation:
    return build_agent_invocation(
        agent_def=_agent_def(),
        provider_name="scripted", model_name="x",
        rendered_system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "wf"),
        user_chat_input_fn=None,
        transcript_dir=tmp_path / "sub",
        events_path=tmp_path / "events.jsonl",
        subagent_defs={},
        tiers=(),
        max_tool_calls=None,
        agent_id="sub:tc-1",
        event_log=event_log,
        change_state=None,
        first_event_parent_id=None,
        resolved_tiers=None,
        coordinator=None,
        subagent_depth=1,
    )


def test_subagent_gets_no_complete_step_tool(tmp_path: Path) -> None:
    tools = _build_tools(_sub_invocation(tmp_path))
    assert "complete_step" not in tools


def test_top_level_keeps_complete_step_tool(tmp_path: Path) -> None:
    inv = _sub_invocation(tmp_path)
    from dataclasses import replace
    tools = _build_tools(replace(inv, subagent_depth=0))
    assert "complete_step" in tools


def test_subagent_system_prompt_has_deliverable_note(tmp_path: Path) -> None:
    inv = _sub_invocation(tmp_path)
    assert SUBAGENT_DELIVERABLE_NOTE in inv.system_prompt
    assert "you MUST call the `complete_step` tool" not in inv.system_prompt


def test_last_assistant_text_walks_back_to_most_recent_text() -> None:
    msgs = [
        Message(role="assistant", content=(TextBlock(text="first"),)),
        Message(role="user", content=(TextBlock(text="u"),)),
        Message(role="assistant", content=(TextBlock(text="second"),)),
        Message(role="assistant", content=()),  # empty terminal turn
    ]
    assert last_assistant_text(msgs) == "second"


def test_last_assistant_text_empty_when_no_text() -> None:
    # The "no deliverable" corner (criterion 5): no assistant text at all ->
    # "" -> provider emits no agent_output -> drive_invocation errors ->
    # dispatch_task raises. The helper is the root of that chain.
    assert last_assistant_text([]) == ""
    assert last_assistant_text(
        [Message(role="assistant", content=()),
         Message(role="user", content=(TextBlock(text="u"),))]
    ) == ""


class _FakeRunner:
    def __init__(self, events) -> None:
        self._events = events

    async def run(self, _inv):
        for e in self._events:
            yield e

    async def interrupt(self) -> None: ...


@pytest.mark.anyio
async def test_drive_invocation_skips_validation_for_subagent(tmp_path: Path) -> None:
    inv = _sub_invocation(tmp_path)
    # Payload {"text": ...} would FAIL the status-enum schema; the subagent path
    # must skip validation and return it as success.
    runner = _FakeRunner([{"type": "agent_output", "payload": {"text": "hi"}}])
    result = await drive_invocation(
        inv, runner, coordinator=None, log=None, emit_events=False)
    assert result.errored is False
    assert result.output == {"text": "hi"}


@pytest.mark.anyio
async def test_scripted_subagent_bare_turn_end_emits_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = {"default": [{"text": "the conclusion"}]}
    p = tmp_path / "script.json"
    p.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(p))

    runner = ScriptedAgentRunner(model_name="x")
    events = [ev async for ev in runner.run(_sub_invocation(tmp_path))]
    outs = [e for e in events if e["type"] == "agent_output"]
    assert outs and outs[-1]["payload"] == {"text": "the conclusion"}
    assert not any(e["type"] == "error" for e in events)


@pytest.mark.anyio
async def test_scripted_subagent_empty_turn_yields_no_output_then_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Criterion 5 end-to-end: an empty terminal turn -> last_assistant_text ""
    # -> provider emits NO agent_output -> drive_invocation surfaces errored.
    script = {"default": [{"text": ""}]}
    p = tmp_path / "script.json"
    p.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(p))

    runner = ScriptedAgentRunner(model_name="x")
    events = [ev async for ev in runner.run(_sub_invocation(tmp_path))]
    assert not any(e["type"] == "agent_output" for e in events)

    result = await drive_invocation(
        _sub_invocation(tmp_path), ScriptedAgentRunner(model_name="x"),
        coordinator=None, log=None, emit_events=False)
    assert result.errored is True
    assert result.output is None


@pytest.mark.anyio
async def test_dispatch_task_returns_raw_final_text(tmp_path: Path) -> None:
    ctx = ToolContext(project_root=tmp_path,
                      events_path=tmp_path / "events.jsonl")
    ctx.subagent_defs = {"reviewer": _agent_def("reviewer")}
    ctx.provider_name = "anthropic"
    ctx.model_name = "claude-sonnet-4-6"
    ctx.parent_transcript_dir = tmp_path / "steps/p/s"
    ctx.coordinator = InterruptCoordinator()
    ctx.quest_context = QuestContext(
        id="c-1", title="C1", project_root=tmp_path,
        workflow_dir=tmp_path / ".imp" / "wf")

    async def _spawn(_inv):
        return _FakeRunner(
            [{"type": "agent_output", "payload": {"text": "looks good"}}])

    ctx.spawn_subagent = _spawn
    out = await dispatch_task(ctx, "reviewer", "review please")
    assert out == "looks good"  # raw text, NOT json.dumps


def test_sub_agent_description_drops_json_claim() -> None:
    desc = make_task_tool().description
    assert "JSON string" not in desc
    assert "final message" in desc
