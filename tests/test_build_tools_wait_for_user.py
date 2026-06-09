from pathlib import Path

from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc
from imp.providers.anthropic import _build_tools


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


def test_wait_for_user_injected_when_declared():
    tools = _build_tools(_Inv(["complete_step", "wait_for_user"]))
    assert "wait_for_user" in tools


def test_wait_for_user_absent_when_not_declared():
    tools = _build_tools(_Inv(["complete_step"]))
    assert "wait_for_user" not in tools
