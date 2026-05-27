"""Claude Code built-in tool names (spec §6.11).

The Claude Code runner passes this list as `--disallowedTools` so the agent
can only invoke `mcp__imp__*` tools (the imp tool registry exposed via an
in-process MCP server). If claude adds a new built-in and this constant
doesn't include it, the agent could silently call the new tool and bypass
imp's strict-MCP-only policy.

The drift guard (`tests/test_cc_builtins_drift.py`) keeps this in sync with
the installed claude CLI. When the SDK pin bumps, run that test; failures
mean a built-in was added (or, less likely, removed).

Source of truth: claude-code's Tool.ts registrations at SDK pin time.
Pin: claude-agent-sdk>=0.2.87,<1.0
"""

from __future__ import annotations

# Sorted alphabetically for easy diffing on SDK bumps.
ALL_CLAUDE_CODE_BUILTIN_TOOLS: tuple[str, ...] = (
    "Bash",
    "BashOutput",
    "Edit",
    "ExitPlanMode",
    "Glob",
    "Grep",
    "KillShell",
    "NotebookEdit",
    "NotebookRead",
    "Read",
    "SlashCommand",
    "Task",
    "TodoWrite",
    "WebFetch",
    "WebSearch",
    "Write",
)

__all__ = ["ALL_CLAUDE_CODE_BUILTIN_TOOLS"]
