from __future__ import annotations

import json
from pathlib import Path

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.scripted import ScriptedAgentRunner


def _agent_def() -> AgentDef:
    return AgentDef(
        name="gate", description="",
        tools=frozenset({"wait_for_user", "complete_step"}), subagents=(),
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
    return AgentInvocation(
        agent_def=_agent_def(),
        provider_name="scripted", model_name="x",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        fs_permissions=(FsRule(operations=("read",), paths=("**",), mode="allow",
                               source=SourceLoc(file=Path("w.yaml"), line=1)),),
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=event_log,
        user_input_fn=user_input_fn,
    )


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_scripted_wait_for_user_consumes_wired_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = {"default": [
        {"text": "asking", "tools": [{"name": "wait_for_user", "input": {}}]},
        {"text": "done",
         "tools": [{"name": "complete_step", "input": {"outcome": "ok", "status": "Success"}}]},
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
    assert "agent_output" in kinds            # the run continued past the gate
    assert received == [""]                    # the wired reply channel WAS called
    log_types = [e["type"] for e in log.read()]
    assert "awaiting_user_input" in log_types  # the gate event was emitted
