from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, QuestContext, Message, TextBlock
from imp.engine.completion import build_continuation_fn
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.scripted import ScriptedAgentRunner


def _agent_def() -> AgentDef:
    return AgentDef(
        name="gate", description="",
        tools=frozenset({"complete_step"}), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum",
                                values=("Success", "Error"), required=True),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        prompt_template_source="hi",
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


def _inv(tmp_path: Path, *, event_log, user_input_fn) -> AgentInvocation:
    cont = build_continuation_fn(
        user_input_fn=user_input_fn,
        event_log=event_log,
        agent_id="step:p/s",
    )
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="scripted", model_name="x",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=event_log,
        user_input_fn=user_input_fn,
        continuation_fn=cont,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_scripted_bare_turn_end_solicits_then_continues(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Turn 0 ends on assistant TEXT with no tool call — the bare turn-end yield.
    # Turn 1 finishes with complete_step once the human reply lands.
    script = {"default": [
        {"text": "asking"},
        {"text": "done",
         "tools": [{"name": "complete_step",
                    "input": {"outcome": "ok", "status": "Success"}}]},
    ]}
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script_path))

    log = InMemoryEventLog()
    received: list[str] = []

    def reply(_query: str) -> str:
        received.append(_query)
        return "continue please"

    runner = ScriptedAgentRunner(model_name="x")
    events = [ev async for ev in runner.run(
        _inv(tmp_path, event_log=log, user_input_fn=reply))]

    kinds = [e["type"] for e in events]
    assert "agent_output" in kinds                  # run continued past the yield
    assert received == [""]                          # the channel WAS solicited
    log_types = [e["type"] for e in log.read()]
    assert "awaiting_user_input" in log_types        # the yield event was emitted


@pytest.mark.anyio
async def test_scripted_bare_turn_end_noop_without_continuation_fn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # No continuation_fn wired (direct-construction default): a bare text-only
    # turn stays a silent no-op — no solicit, no awaiting_user_input — and the
    # script exhausts to the agent_output_missing error.
    script = {"default": [{"text": "asking"}]}
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script_path))

    log = InMemoryEventLog()
    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="scripted", model_name="x",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
    )
    runner = ScriptedAgentRunner(model_name="x")
    events = [ev async for ev in runner.run(inv)]

    errs = [e for e in events if e["type"] == "error"]
    assert errs and errs[-1]["payload"]["category"] == "agent_output_missing"
    assert "awaiting_user_input" not in [e["type"] for e in log.read()]



