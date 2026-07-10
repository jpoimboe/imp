from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.runner import AgentInvocation, QuestContext
from imp.engine.types import AgentDef, OutputSchema, SourceLoc
from imp.providers.claude_code import ClaudeCodeAgentRunner

_BLANKED_KEYS = (
    "MAX_THINKING_TOKENS",
    "CLAUDE_CODE_DISABLE_THINKING",
    "CLAUDE_CODE_DISABLE_ADAPTIVE_THINKING",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS",
    "CLAUDE_CODE_EFFORT_LEVEL",
    "USER_TYPE",
)


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


def test_spawn_env_blanks_inherited_thinking_and_effort_vars(tmp_path: Path) -> None:
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    opts = runner._build_options(
        _invocation(tmp_path), mcp_server=object(), tool_names=["bash"]
    )
    for key in _BLANKED_KEYS:
        assert opts.env.get(key) == "", (
            f"expected options.env[{key!r}] == '' to override any inherited value, "
            f"got {opts.env.get(key)!r}"
        )


def test_hostile_host_env_does_not_change_spawn_posture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A hostile host env must not leak into the subprocess: the SDK merges
    # options.env OVER the inherited env, so the blanked keys win and the
    # imp-owned posture stands.
    monkeypatch.setenv("MAX_THINKING_TOKENS", "99999")
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "low")
    monkeypatch.setenv("USER_TYPE", "external")
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    opts = runner._build_options(
        _invocation(tmp_path, tier="medium"), mcp_server=object(), tool_names=["bash"]
    )
    assert opts.env.get("MAX_THINKING_TOKENS") == ""
    assert opts.env.get("CLAUDE_CODE_EFFORT_LEVEL") == ""
    assert opts.env.get("USER_TYPE") == ""
    # Posture stays imp-owned regardless of the host env.
    assert opts.thinking == {"type": "adaptive", "display": "summarized"}
    assert opts.effort == "high"
