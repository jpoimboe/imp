from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers import anthropic, claude_code, openai

PROVIDERS = [anthropic, openai, claude_code]


def _agent(tools):
    return AgentDef(
        name="x",
        description="",
        prompt_template_source="hi",
        tools=frozenset(tools),
        output_schema=OutputSchema(
            fields=(OutputField(name="done", type="bool"),),
            source=SourceLoc(file=Path("a.md"), line=1),
        ),
        subagents=(),
        source_path=Path("a.md"),
        source=SourceLoc(file=Path("a.md"), line=1),
    )


class _Inv:
    def __init__(self, tools):
        self.agent_def = _agent(tools)
        self.provider_name = "anthropic"
        self.tiers = ()


@pytest.mark.parametrize("provider", PROVIDERS)
def test_wait_for_user_injected_when_declared(provider):
    tools = provider._build_tools(_Inv(["complete_step", "wait_for_user"]))
    assert "wait_for_user" in tools


@pytest.mark.parametrize("provider", PROVIDERS)
def test_wait_for_user_absent_when_not_declared(provider):
    tools = provider._build_tools(_Inv(["complete_step"]))
    assert "wait_for_user" not in tools
