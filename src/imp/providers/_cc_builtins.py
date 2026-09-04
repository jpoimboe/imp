# SPDX-License-Identifier: MIT
# Copyright 2026 Josh Poimboeuf
"""Claude Code built-in tool names (spec §6.11).

The Claude Code runner passes this list as `--disallowedTools` so the agent
can only invoke `mcp__imp__*` tools (the imp tool registry exposed via an
in-process MCP server). If claude adds a new built-in and this constant
doesn't include it, the agent could silently call the new tool and bypass
imp's strict-MCP-only policy.

The drift guard (`tests/test_cc_builtins_drift_live.py`) spawns the bundled CLI
under the exact deny list this constant produces and reads the tool registry
it advertises, rather than syncing against a bare CLI listing. A failure means
a built-in is reachable from every claude_code step; add it here.

Source of truth: the tool registry advertised by the CLI bundled with the
pinned SDK — not the `claude` on PATH, which the SDK never spawns.
Pin: claude-agent-sdk>=0.2.87,<1.0
"""

from __future__ import annotations

# Sorted alphabetically for easy diffing on SDK bumps.
ALL_CLAUDE_CODE_BUILTIN_TOOLS: tuple[str, ...] = (
    "AskUserQuestion",
    "Bash",
    "CronCreate",
    "CronDelete",
    "CronList",
    "DesignSync",
    "Edit",
    "EnterPlanMode",
    "EnterWorktree",
    "ExitPlanMode",
    "ExitWorktree",
    "Glob",
    "Grep",
    "ListAgents",
    "Monitor",
    "NotebookEdit",
    "PushNotification",
    "Read",
    "RemoteTrigger",
    "ReportFindings",
    "ScheduleWakeup",
    "SendMessage",
    "Skill",
    "Task",
    "TaskCreate",
    "TaskGet",
    "TaskList",
    "TaskOutput",
    "TaskStop",
    "TaskUpdate",
    "ToolSearch",
    "WebFetch",
    "WebSearch",
    "Workflow",
    "Write",
)

__all__ = ["ALL_CLAUDE_CODE_BUILTIN_TOOLS"]
