"""Regression: the claude_code provider must pin the `claude` subprocess to
the change's project_root.

Bug: `_build_options` never set `ClaudeAgentOptions.cwd`. The SDK then launches
`claude` with `cwd=None` (inherit) and, critically, does NOT override `$PWD`
(claude_agent_sdk subprocess_cli.py only sets `process_env["PWD"]` when `cwd`
is truthy). So `claude` inherited whatever stale `$PWD` the imp process had.

In the dogfood test the imp process was spawned via `Popen(cwd=tmp_path,
env=dict(os.environ))` — which changes the child's working directory but leaves
`$PWD` pointing at pytest's launch dir (imp's *own* repo). claude keyed off
`$PWD`, so it edited and committed the real repo instead of tmp_path: the test
saw only the `initial` commit in tmp_path ("expected >=2 commits, got 1") while
the coder's commit landed on the real master.

Fix: pass `cwd=change_context.project_root` so the SDK sets both the subprocess
cwd and `$PWD` to the project root — independent of where imp was launched.
"""

from __future__ import annotations

from pathlib import Path

from imp.agents.runner import AgentInvocation, ChangeContext
from imp.engine.types import (
    AgentDef,
    OutputSchema,
    SourceLoc,
)
from imp.providers.claude_code import ClaudeCodeAgentRunner


def _invocation(project_root: Path) -> AgentInvocation:
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
        change_context=ChangeContext(
            title="Test",
            project_root=project_root,
            workflow_dir=project_root / ".imp" / "change" / "workflow",
        ),
        transcript_dir=project_root / ".imp" / "steps",
        events_path=project_root / "events.jsonl",
    )


def test_build_options_pins_cwd_to_project_root(tmp_path: Path) -> None:
    """`claude` must be launched at the change's project_root so it cannot
    inherit a stale $PWD and operate on the wrong repo."""
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    opts = runner._build_options(
        _invocation(tmp_path), mcp_server=object(), tool_names=["bash"]
    )
    assert str(opts.cwd) == str(tmp_path), (
        f"expected ClaudeAgentOptions.cwd == project_root {tmp_path!r}, "
        f"got {opts.cwd!r}; claude would inherit a stale $PWD and edit/commit "
        f"the wrong working tree (see dogfood pollution regression)."
    )


def test_build_options_blanks_external_anthropic_keys(tmp_path: Path) -> None:
    """The nested `claude` authenticates via its own backend (OAuth/Vertex/
    Bedrock), never via ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN (those are the
    direct-SDK `anthropic` provider's external-key path). If the parent env
    exports either, claude switches to it as an external key and a bogus/
    mismatched value fails with "Invalid API key" (exit 0 → AuthError → empty
    review). The SDK merges options.env OVER the inherited env, so _build_options
    must blank both to make the subprocess immune to whatever the parent exports.
    """
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    opts = runner._build_options(
        _invocation(tmp_path), mcp_server=object(), tool_names=["bash"]
    )
    assert opts.env.get("ANTHROPIC_API_KEY") == "", (
        f"expected options.env['ANTHROPIC_API_KEY']=='' to override any inherited "
        f"external key, got {opts.env.get('ANTHROPIC_API_KEY')!r}"
    )
    assert opts.env.get("ANTHROPIC_AUTH_TOKEN") == "", (
        f"expected options.env['ANTHROPIC_AUTH_TOKEN']=='' to override any inherited "
        f"external token, got {opts.env.get('ANTHROPIC_AUTH_TOKEN')!r}"
    )


def test_build_options_isolates_config_dir_for_fresh(tmp_path: Path) -> None:
    """A fresh start passes config_dir: _build_options must point CLAUDE_CONFIG_DIR
    at it and DROP the history-suppression flags (the isolated home makes them
    pointless, and they starve session_store of the conversation body)."""
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    cfg = tmp_path / "iso-home"
    opts = runner._build_options(
        _invocation(tmp_path), mcp_server=object(), tool_names=["bash"],
        config_dir=cfg,
    )
    assert opts.env.get("CLAUDE_CONFIG_DIR") == str(cfg)
    assert opts.env.get("ANTHROPIC_API_KEY") == ""
    assert "CLAUDE_CODE_SKIP_PROMPT_HISTORY" not in opts.env
    assert opts.extra_args == {}

    # The SDK mirror batcher resolves projects_dir from options.env; it MUST
    # land under the isolated dir, or mirroring silently stops and resume breaks.
    from claude_agent_sdk._internal.sessions import _get_projects_dir
    assert _get_projects_dir(opts.env) == cfg / "projects"


def test_build_options_drops_suppression_flags_when_no_config_dir(
    tmp_path: Path,
) -> None:
    """A config_dir=None build (native resume) no longer adds the history
    suppression flags: claude-p stays off ~/.claude via the SDK's own
    materialized temp dir, not CLAUDE_CODE_SKIP_PROMPT_HISTORY /
    --no-session-persistence. Both must be absent and extra_args must be {}."""
    runner = ClaudeCodeAgentRunner("claude-sonnet-4-5")
    opts = runner._build_options(
        _invocation(tmp_path), mcp_server=object(), tool_names=["bash"]
    )
    assert "CLAUDE_CODE_SKIP_PROMPT_HISTORY" not in opts.env, (
        f"suppression flag must be gone, got "
        f"{opts.env.get('CLAUDE_CODE_SKIP_PROMPT_HISTORY')!r}"
    )
    assert "CLAUDE_CONFIG_DIR" not in opts.env, (
        "config_dir=None must not set CLAUDE_CONFIG_DIR"
    )
    assert opts.extra_args == {}, (
        f"expected no --no-session-persistence, got extra_args={opts.extra_args!r}"
    )
