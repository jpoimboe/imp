"""Gated live elicitation (spec AC43). Billable; opt in via IMP_LIVE_PROVIDERS
(`anthropic` and/or `openai` — each variant skips unless its provider is listed).

Confirms the task-tool description actually elicits >=2 task calls in ONE
assistant message from a real model on BOTH in-process providers, with schema-valid
args on the openai variant (parallel_tool_calls + strict). Skipped in CI.
"""
from __future__ import annotations

import pytest

from tests._live import opted_in


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
@pytest.mark.parametrize("provider", [
    pytest.param("anthropic", marks=pytest.mark.skipif(
        not opted_in("anthropic"), reason="anthropic not in IMP_LIVE_PROVIDERS")),
    pytest.param("openai", marks=pytest.mark.skipif(
        not opted_in("openai"), reason="openai not in IMP_LIVE_PROVIDERS")),
])
async def test_independent_subtasks_elicit_parallel_task_calls(provider: str) -> None:
    # A prompt describing two INDEPENDENT subtasks should produce >=2 task tool_use
    # blocks in a single assistant message (the elicitation language at work).
    from imp.agents.runner import ToolUseBlock
    from tests.live_helpers import collect_first_assistant_tool_uses

    tool_uses = await collect_first_assistant_tool_uses(
        provider=provider,
        prompt=(
            "Dispatch TWO fully independent subtasks now, in parallel, "
            "using the task tool: one to subagent 'a' and one to subagent 'b'. "
            "They do not depend on each other."
        ),
    )
    task_uses = [b for b in tool_uses
                 if isinstance(b, ToolUseBlock) and b.name == "sub_agent"]
    assert len(task_uses) >= 2
    if provider == "openai":
        for b in task_uses:
            assert isinstance(b.input.get("subagent_name"), str)
            assert isinstance(b.input.get("task_description"), str)
