from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers import anthropic, claude_code, openai, scripted

# wait_for_user is no longer a registered tool on the real model providers: a
# bare turn-end is the yield, so the live LLM is never advertised the tool. The
# offline scripted provider still synthesizes it (its fixtures inject the call).
NO_INJECT = [anthropic, openai, claude_code]
INJECT = [scripted]


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


@pytest.mark.parametrize("provider", NO_INJECT)
def test_wait_for_user_not_injected_on_model_providers(provider):
    tools = provider._build_tools(_Inv(["complete_step", "wait_for_user"]))
    assert "wait_for_user" not in tools


@pytest.mark.parametrize("provider", INJECT)
def test_wait_for_user_injected_when_declared(provider):
    tools = provider._build_tools(_Inv(["complete_step", "wait_for_user"]))
    assert "wait_for_user" in tools


@pytest.mark.parametrize("provider", NO_INJECT + INJECT)
def test_wait_for_user_absent_when_not_declared(provider):
    tools = provider._build_tools(_Inv(["complete_step"]))
    assert "wait_for_user" not in tools
