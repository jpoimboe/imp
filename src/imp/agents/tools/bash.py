"""bash tool (spec §6.6).

Plan 1 ships a SYNC `subprocess.run` wrapper for simplicity. This blocks the
event loop for the duration of the shell command — the runner can't poll
`self._cancel` until bash returns. Plan 3's interrupt-mechanics hardening
(§6.15) replaces this with `asyncio.create_subprocess_exec` + a process-group
SIGTERM→2s→SIGKILL escalation path so the runner can interrupt long-running
shell commands mid-execution.
"""

from __future__ import annotations

import json
import subprocess

from imp.agents.tools import imp_tool
from imp.agents.tools.context import ToolContext


@imp_tool(name="bash", description="Run a shell command. Returns JSON {stdout, stderr, returncode}. No permission gate (shell access is total at OS-user level).")
def bash(ctx: ToolContext, command: str, cwd: str | None = None) -> str:
    proc = subprocess.run(
        command,
        shell=True,
        cwd=cwd or str(ctx.project_root),
        capture_output=True,
        text=True,
        check=False,
    )
    return json.dumps({
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    })
