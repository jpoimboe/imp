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
    FsRule,
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
            change_id=1,
            project_root=project_root,
            artifacts_dir=project_root / ".imp" / "artifacts",
        ),
        interactive=False,
        transcript_dir=project_root / ".imp" / "steps",
        fs_permissions=(
            FsRule(
                operations=("read", "write"),
                paths=("/project/**",),
                mode="allow",
                source=loc,
            ),
        ),
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
