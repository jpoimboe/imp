"""ToolContext (spec §6.6) — per-invocation state passed to every tool.

Virtual path conventions:
  /project/<rel>   → <project_root>/<rel>
  /scratch/<rel>   → ToolContext.scratch dict (in-memory; lives for the step)
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from imp.agents.permissions import check_permission
from imp.engine.types import AgentDef, FsRule


@dataclass
class ToolContext:
    project_root: Path
    fs_permissions: tuple[FsRule, ...]
    events_path: Path
    scratch: dict[str, str] = field(default_factory=dict)
    todos: list[dict[str, Any]] = field(default_factory=list)
    # Subagent dispatch fills these:
    subagent_defs: dict[str, AgentDef] = field(default_factory=dict)
    provider_name: str = ""
    model_name: str = ""
    parent_transcript_dir: Path | None = None
    interactive: bool = False
    running_processes: list[Any] = field(default_factory=list)
    # The change being processed; used by subagent dispatch to propagate change_id.
    change_context: Any = None
    # Live State of the running Change; the rename_change tool sets
    # pending_rename on it. None for subagents (no rename authority).
    change_state: Any = None
    # The runner injects this callable so the PROMPT_USER resume path can read
    # the user's reply when an interactive transcript ends on assistant text.
    user_input_fn: Any = None
    # Shared EventLog WRITE instance, set by the parent runner at the same site
    # it sets spawn_subagent. dispatch_task threads this into the subagent's
    # AgentInvocation so subagent audit events land in the parent's log + fire
    # the parent's on_event. Distinct from the read-only EventLog dispatch_task
    # builds for prompt-context rendering.
    event_log: Any = None
    # Subagent dispatch hook (filled by runner; signature defined in subagents tool).
    spawn_subagent: Any = None
    # role → ProviderModel; copied from invocation.resolved_roles by the runner.
    resolved_roles: dict[str, Any] | None = None
    # The run's effective model-tier menu; copied from invocation.model_tiers by
    # the provider. dispatch_task reads it only to name valid tiers on error.
    model_tiers: tuple[str, ...] = ()
    # invocation_id counter for disambiguating same-named subagent invocations
    _subagent_counter: int = 0

    def next_subagent_invocation_id(self) -> int:
        self._subagent_counter += 1
        return self._subagent_counter

    def resolve_path(self, virtual: str) -> Path | None:
        """Returns a real Path or None for /scratch/* (caller uses self.scratch)."""
        if virtual.startswith("/project/"):
            return self.project_root / virtual[len("/project/"):]
        if virtual.startswith("/scratch/") or virtual == "/scratch":
            return None
        raise ValueError(
            f"virtual path must start with /project/ or /scratch/; got {virtual!r}"
        )

    def check_fs_permission(self, op: Literal["read", "write"], virtual: str) -> None:
        check_permission(self.fs_permissions, op, virtual)


async def terminate_running_processes(
    ctx: ToolContext, *, timeout: float = 2.0,
) -> list[Any]:
    """Send SIGTERM to every registered process, wait `timeout`, then SIGKILL
    survivors. Drain the registry. Returns the list of processes that were
    signalled (caller emits tool_killed events from it)."""
    import anyio

    if not ctx.running_processes:
        return []
    procs = list(ctx.running_processes)
    ctx.running_processes.clear()

    for p in procs:
        # `.terminate()` sends SIGTERM (or maps to it on POSIX). Ignore errors
        # for already-exited processes.
        with contextlib.suppress(ProcessLookupError, AttributeError):
            p.terminate()

    # Wait up to `timeout` seconds for all to exit; whatever's still alive
    # gets SIGKILL.
    async def _wait_one(p: Any) -> None:
        with contextlib.suppress(Exception):
            await p.wait()

    with anyio.move_on_after(timeout):
        async with anyio.create_task_group() as tg:
            for p in procs:
                tg.start_soon(_wait_one, p)

    for p in procs:
        if getattr(p, "returncode", None) is None:
            with contextlib.suppress(ProcessLookupError, AttributeError):
                p.kill()
            with contextlib.suppress(Exception):
                await p.wait()
    return procs
