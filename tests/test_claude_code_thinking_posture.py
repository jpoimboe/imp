from __future__ import annotations

import dataclasses as dc
from pathlib import Path

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.types import AgentDef, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner


def _invocation(project_root: Path, tier: str | None = None) -> AgentInvocation:
    loc = SourceLoc(file=Path("w.yaml"), line=1)
    agent_def = AgentDef(
        name="coder",
        description="",
        tools=frozenset({"bash"}),
        subagents=(),
        output_schema=OutputSchema(fields=(), source=loc),
        prompt_template_source="",
        source_path=Path("coder.md"),
        source=loc,
    )
    return AgentInvocation(
        agent_def=agent_def,
        provider_name="claude_code",
        model_name="claude-sonnet-4-5",
        system_prompt="be a coder",
        initial_messages=(),
        quest_context=QuestContext(
            title="Test",
            project_root=project_root,
            workflow_dir=project_root / ".imp" / "change" / "workflow",
        ),
        transcript_dir=project_root / ".imp" / "steps",
        events_path=project_root / "events.jsonl",
        tier=tier,
    )


def test_sdk_options_expose_thinking_and_effort_fields() -> None:
    """Guard: the posture pin depends on first-class ClaudeAgentOptions fields,
    not an out-of-band env string."""
    from claude_agent_sdk import ClaudeAgentOptions

    names = {f.name for f in dc.fields(ClaudeAgentOptions)}
    assert "thinking" in names
    assert "effort" in names


def test_low_tier_pins_disabled_thinking_and_no_effort(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    opts = runner._build_options(
        _invocation(tmp_path, tier="low"), mcp_server=object(), tool_names=["bash"]
    )
    assert opts.thinking == {"type": "disabled"}
    assert opts.effort is None


def test_medium_tier_pins_adaptive_thinking_and_high_effort(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    opts = runner._build_options(
        _invocation(tmp_path, tier="medium"), mcp_server=object(), tool_names=["bash"]
    )
    assert opts.thinking == {"type": "adaptive", "display": "summarized"}
    assert opts.effort == "high"


def test_high_tier_pins_adaptive_thinking_and_high_effort(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner("claude-opus-4-8")
    opts = runner._build_options(
        _invocation(tmp_path, tier="high"), mcp_server=object(), tool_names=["bash"]
    )
    assert opts.thinking == {"type": "adaptive", "display": "summarized"}
    assert opts.effort == "high"


def test_untiered_invocation_leaves_posture_unpinned(tmp_path: Path) -> None:
    # A legacy/untiered (tier=None) invocation keeps the claude-p default: no
    # thinking pin and no effort pin. This preserves today's behavior for
    # subagents that inherit a parent provider without an explicit tier.
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    opts = runner._build_options(
        _invocation(tmp_path, tier=None), mcp_server=object(), tool_names=["bash"]
    )
    assert opts.thinking is None
    assert opts.effort is None
