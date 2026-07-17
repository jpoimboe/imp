from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, Message, QuestContext, TextBlock
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.types import AgentDef
from imp.providers.anthropic import _build_tools as anthropic_build

DENIED = frozenset({"rename_quest", "create_and_enter_worktree", "save_and_exit"})
REVIEW_AGENTS = (
    "spec-reviewer",
    "plan-reviewer",
    "gap-check",
    "integration-review",
    "batch-code-reviewer",
)


@pytest.fixture(scope="module")
def imp_workflow():
    return load_workflow(bundled_workflow_dir("imp"))


@pytest.mark.parametrize("name", REVIEW_AGENTS)
def test_review_agent_effective_toolset_omits_top_level_caps(imp_workflow, name):
    tools = imp_workflow.agents[name].tools
    assert DENIED.isdisjoint(tools), f"{name} still holds {DENIED & tools}"
    # Falsifiability guard: the deny subtracts only the three caps — a read tool survives.
    assert "read_file" in tools


def _inv(agent: AgentDef) -> AgentInvocation:
    return AgentInvocation(
        agent_def=agent, provider_name="anthropic", model_name="m",
        system_prompt="s",
        initial_messages=(Message(role="user", content=(TextBlock(text="go"),)),),
        quest_context=QuestContext(title="t", project_root=Path("/"),
                                    workflow_dir=Path("/")),
        transcript_dir=Path("/"), events_path=Path("/events.jsonl"))


@pytest.mark.parametrize("name", REVIEW_AGENTS)
def test_review_agent_assembled_schema_omits_top_level_caps(imp_workflow, name):
    built = anthropic_build(_inv(imp_workflow.agents[name]))
    for denied in DENIED:
        assert denied not in built, f"{name} schema still contains {denied}"
    # Sanity: the build produced a real toolset, so absence above is meaningful.
    assert "read_file" in built
