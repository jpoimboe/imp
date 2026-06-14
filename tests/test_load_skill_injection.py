# tests/test_load_skill_injection.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.engine.types import OutputField, OutputSchema, SourceLoc
from imp.providers import anthropic, claude_code, openai

PROVIDERS = [anthropic, openai, claude_code]

_SCHEMA = OutputSchema(
    fields=(OutputField(name="status", type="enum",
                        values=("Success",), required=True),),
    source=SourceLoc(file=Path("a.md"), line=1),
)


class _AgentDef:
    def __init__(self, tools: tuple[str, ...]) -> None:
        self.tools = frozenset(tools)
        self.output_schema = _SCHEMA
        self.explicit_tools = bool(tools)
        self.deny_tools: frozenset[str] = frozenset()


class _Inv:
    def __init__(self, tools: tuple[str, ...], provider: str = "anthropic") -> None:
        self.agent_def = _AgentDef(tools)
        self.tiers: tuple[str, ...] = ()
        self.provider_name = provider


@pytest.mark.parametrize("provider", PROVIDERS)
def test_load_skill_injected_without_declaration(provider) -> None:
    tools = provider._build_tools(_Inv(tools=()))
    assert "load_skill" in tools
    assert "complete_step" in tools


@pytest.mark.parametrize("provider", PROVIDERS)
def test_redundant_load_skill_declaration_does_not_raise(provider) -> None:
    # Author writing tools: [load_skill] must not trip the unknown-tool KeyError.
    tools = provider._build_tools(_Inv(tools=("load_skill",)))
    assert "load_skill" in tools
