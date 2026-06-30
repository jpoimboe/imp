from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, ChangeContext, Message, TextBlock
from imp.agents.transcript import Transcript
from imp.cli.inert_resume import make_buffered_solicit
from imp.engine.completion import build_continuation_fn
from imp.engine.state import InMemoryEventLog
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.scripted import ScriptedAgentRunner


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


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


@pytest.mark.anyio
async def test_resumed_run_supports_multiple_exchanges_to_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Turn 0 and turn 1 each end on assistant TEXT with no tool call (two
    # bare-turn-end yields); turn 2 finishes with complete_step.
    script = {"default": [
        {"text": "first question?"},
        {"text": "follow-up question?"},
        {"text": "done",
         "tools": [{"name": "complete_step",
                    "input": {"outcome": "ok", "status": "Success"}}]},
    ]}
    script_path = tmp_path / "script.json"
    script_path.write_text(json.dumps(script))
    monkeypatch.setenv("IMP_SCRIPTED_SCRIPT", str(script_path))

    log = InMemoryEventLog()
    inner_calls: list[str] = []

    async def inner(prompt: str) -> str:
        inner_calls.append(prompt)
        return "follow-up answer"

    # The buffered solicit delivers the resume first line, then delegates every
    # later solicit to the interactive reader — the exact mechanism a resumed
    # run relies on to stay live across exchanges.
    solicit: Callable[[str], Awaitable[str]] = make_buffered_solicit(
        "first reply", inner)
    cont = build_continuation_fn(
        user_input_fn=solicit,
        event_log=log,
        agent_id="step:p/s",
    )
    inv = AgentInvocation(
        agent_def=_agent_def(),
        provider_name="scripted", model_name="x",
        system_prompt="be terse",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        change_context=ChangeContext(
            title="Test", project_root=tmp_path,
            workflow_dir=tmp_path / ".imp" / "change" / "workflow"),
        transcript_dir=tmp_path / "step",
        events_path=tmp_path / "events.jsonl",
        agent_id="step:p/s",
        event_log=log,
        user_input_fn=solicit,
        continuation_fn=cont,
    )

    runner = ScriptedAgentRunner(model_name="x")
    events = [ev async for ev in runner.run(inv)]

    # The run reached complete_step: it did NOT drop to the shell between
    # exchanges (the p9ebp1 regression).
    assert "agent_output" in [e["type"] for e in events]
    # The inner (interactive) reader was consulted exactly once — for the SECOND
    # exchange. The first reply came from the buffer, not the reader.
    assert inner_calls == [""]
    # Both replies were delivered as user messages in the same step's transcript.
    msgs = Transcript(tmp_path / "step" / "transcript.jsonl").load()
    user_texts = [
        b.text for m in msgs if m.role == "user"
        for b in m.content if isinstance(b, TextBlock)
    ]
    assert "first reply" in user_texts          # buffered first line delivered
    assert "follow-up answer" in user_texts     # delegated follow-up delivered
    # Two yields -> two awaiting_user_input events.
    assert [e["type"] for e in log.read()].count("awaiting_user_input") == 2
