"""ToolContext (spec §6.6) — per-invocation state passed to every tool.

Virtual path conventions:
  /project/<rel>   → <project_root>/<rel>
  /scratch/<rel>   → ToolContext.scratch dict (in-memory; lives for the step)
"""

from __future__ import annotations

import contextlib
import os
import signal as _signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from imp.agents.permissions import check_permission
from imp.agents.tools.limits import PROCESS_KILL_GRACE_S
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
    # The change being processed; used by subagent dispatch to propagate slug/title.
    change_context: Any = None
    # Live State of the running Change; used by the subagent-authority guard
    # (rename_change raises if this is None). None for subagents (no rename authority).
    change_state: Any = None
    # The LIVE, mutable Change (top-level steps only; None for subagents). The
    # rename_change tool mutates change.title + rewrites change.json through it.
    # Distinct from change_context, which is the FROZEN render-time snapshot.
    change: Any = None
    # The runner injects this callable so the PROMPT_USER resume path can read
    # the user's reply when an interactive transcript ends on assistant text.
    user_input_fn: Any = None
    # Shared EventLog WRITE instance, set by the parent runner at the same site
    # it sets spawn_subagent. dispatch_task threads this into the subagent's
    # AgentInvocation so subagent audit events land in the parent's log + fire
    # the parent's on_event. Distinct from the read-only EventLog dispatch_task
    # builds for prompt-context rendering.
    event_log: Any = None
    # The agent_id of THIS invocation (e.g. "step:review/final-review" or a
    # subagent id). Set by each provider where it builds the ToolContext, so a
    # tool that emits an event (wait_for_user) pairs it under the right node.
    agent_id: str | None = None
    # Subagent dispatch hook (filled by runner; signature defined in subagents tool).
    spawn_subagent: Any = None
    # tier → ProviderModel; copied from invocation.resolved_tiers by the runner.
    resolved_tiers: dict[str, Any] | None = None
    # The run's effective tier menu; copied from invocation.tiers by
    # the provider. dispatch_task reads it only to name valid tiers on error.
    tiers: tuple[str, ...] = ()
    # Depth of THIS invocation in the subagent tree (0 = top-level step).
    # Each provider copies it from invocation.subagent_depth; dispatch_task
    # reads it to compute the child's depth and enforce MAX_SUBAGENT_DEPTH.
    subagent_depth: int = 0
    # The run's InterruptCoordinator, copied from invocation.coordinator by the
    # provider. dispatch_task passes it to drive_invocation so a subagent gets
    # pause detection + interrupt attachment. None for legacy/test contexts.
    coordinator: Any = None
    # Optional lint-clean gate command (config.lint_command). Threaded onto the
    # subprocess env (IMP_LINT_COMMAND) by the bash tool. None => no lint gate.
    lint_command: str | None = None
    # Optional test-run gate command (config.test_command). Threaded onto the
    # subprocess env (IMP_TEST_COMMAND) by the bash tool. None => no test gate.
    test_command: str | None = None
    # §10/§12: when True, the run is headless (no human at the keyboard). The
    # subagent backoff loop applies a wall-clock ceiling instead of retrying
    # forever. Copied from invocation.non_interactive by each provider.
    non_interactive: bool = False
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


def _supports_killpg() -> bool:
    # start_new_session (os.setsid) gives the child pgid == pid, so os.killpg can
    # target the whole tree without os.getpgid (which would add a second
    # ProcessLookupError race on a reaped pid). Absent setsid/killpg (Windows /
    # no-setsid), fall back to single-pid termination.
    return hasattr(os, "killpg") and hasattr(os, "setsid")


async def kill_process_group(proc: Any, sig: int) -> None:
    """Send `sig` to the process GROUP of `proc`, falling back to single-pid on
    platforms without setsid/killpg. Suppresses ESRCH (already-reaped). Does NOT
    wait/reap — the caller reaps via `await proc.wait()`.

    M1 INVARIANT: this is correct only because every registered proc is a setsid
    session leader (pgid == pid; see the bash registration site), so os.killpg(pid)
    targets exactly that group. If a future caller ever registers a NON-setsid
    proc, os.killpg(pid, …) could no-op (ESRCH) or signal an unrelated group —
    revisit this then.
    """
    pid = getattr(proc, "pid", None)
    if pid is not None and _supports_killpg():
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, sig)
        return
    fn = proc.terminate if sig == _signal.SIGTERM else proc.kill
    with contextlib.suppress(ProcessLookupError, AttributeError):
        fn()


async def terminate_running_processes(
    ctx: ToolContext, *, timeout: float = PROCESS_KILL_GRACE_S,
) -> list[Any]:
    """SIGTERM the process GROUP of every registered process, wait `timeout`
    (named grace, default 4 s), then SIGKILL the group of any survivor. Drain the
    registry. Returns the processes that were signalled (caller emits tool_killed
    from it). Escalation is TIME-gated, not count-gated."""
    import anyio

    if not ctx.running_processes:
        return []
    procs = list(ctx.running_processes)
    ctx.running_processes.clear()

    for p in procs:
        await kill_process_group(p, _signal.SIGTERM)

    async def _wait_one(p: Any) -> None:
        with contextlib.suppress(Exception):
            await p.wait()

    with anyio.move_on_after(timeout):
        async with anyio.create_task_group() as tg:
            for p in procs:
                tg.start_soon(_wait_one, p)

    for p in procs:
        if getattr(p, "returncode", None) is None:
            await kill_process_group(p, _signal.SIGKILL)
            with contextlib.suppress(Exception):
                await p.wait()
    return procs
