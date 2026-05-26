# tests/test_openai_runner.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import (
    AgentInvocation,
    ChangeContext,
    Message,
    TextBlock,
)
from imp.agents.tools import get_registry  # noqa: F401 ensures tools registered
from imp.engine.types import AgentDef, FsRule, OutputField, OutputSchema, SourceLoc
from imp.providers.openai import OpenAIAgentRunner
from tests.openai_fakes import FakeAsyncOpenAI, completion, message, tool_call


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _agent_def(tools=frozenset({"complete_step", "read_file"})) -> AgentDef:
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


def _inv(tmp_path: Path, tools=None) -> AgentInvocation:
    return AgentInvocation(
        agent_def=_agent_def(tools or frozenset({"complete_step", "read_file"})),
        provider_name="openai", model_name="gpt-4o-mini",
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
    fake = FakeAsyncOpenAI([
        completion(message(
            content="calling complete_step",
            tool_calls=[tool_call("c1", "complete_step",
                                  {"status": "Success", "note": "ok"})],
        )),
    ])

    runner = OpenAIAgentRunner(
        model_name="gpt-4o-mini",
        client_factory=lambda: fake,
    )
    events = []
    async for ev in runner.run(_inv(tmp_path)):
        events.append(ev)

    kinds = [e["type"] for e in events]
    assert "agent_message" in kinds
    assert kinds[-1] == "agent_output"
    assert events[-1]["payload"] == {"status": "Success", "note": "ok"}
