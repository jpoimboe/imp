from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers import anthropic, claude_code, openai, scripted

# wait_for_user is no longer a registered tool on ANY provider: a bare turn-end
# is the yield. Even the offline scripted provider routes its bare turn-end
# through the shared continuation handler instead of a wait_for_user tool_use.
PROVIDERS = [anthropic, openai, claude_code, scripted]


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
def test_wait_for_user_not_a_registered_tool(provider):
    tools = provider._build_tools(_Inv(["complete_step", "wait_for_user"]))
    assert "wait_for_user" not in tools


@pytest.mark.parametrize("provider", PROVIDERS)
def test_wait_for_user_absent_when_not_declared(provider):
    tools = provider._build_tools(_Inv(["complete_step"]))
    assert "wait_for_user" not in tools


def test_make_wait_for_user_tool_no_longer_exists():
    import imp.agents.tools.wait_for_user as wfu
    assert not hasattr(wfu, "make_wait_for_user_tool")
