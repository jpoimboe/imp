"""git_diff (spec §6.6)."""

from __future__ import annotations

import subprocess

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext


@imp_tool(
    name="git_diff",
    description="Run `git diff` (or `git diff --staged`) in the project root.",
)
def git_diff(ctx: ToolContext, staged: bool = False) -> str:
    cmd = ["git", "diff"]
    if staged:
        cmd.append("--staged")
    try:
        proc = subprocess.run(
            cmd, cwd=ctx.project_root, capture_output=True, text=True, check=False,
        )
    except FileNotFoundError:
        return "error: git not installed"
    if proc.returncode != 0 and proc.stderr:
        return f"error: {proc.stderr.strip()}"
    return proc.stdout
