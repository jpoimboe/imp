"""ToolContext (spec §6.6) — per-invocation state passed to every tool.

Path model: in-worktree files are addressed by paths RELATIVE to the worktree
root. `resolve_path` resolves both relative-under-root and absolute paths with NO
containment raise; `authorize_fs` then bounds the result against the sandbox
policy so the fs tools reach the same paths as `bash`. Tool paths are
worktree-relative. No virtual `/project`/`/scratch` namespace; system paths are
`bash`'s job, backstopped by the OS sandbox.
"""

from __future__ import annotations

import contextlib
import os
import signal as _signal
from collections.abc import AsyncIterator, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from imp.agents.tools.errors import PermissionDenied
from imp.agents.tools.limits import PROCESS_KILL_GRACE_S
from imp.engine.env import TestGates
from imp.engine.types import AgentDef


@dataclass(frozen=True)
class ReadRecord:
    """Per-step record of a successful read, for the read-before-write gate and
    read dedup.

    content_sha256 hashes the RAW file bytes (not read_file's lossy decoded
    text). full is True iff the read reached EOF and was not cap-truncated.
    rendered maps each (offset, limit) already shown for THIS file version this
    step to the true (first_line, last_line) span it produced; mutated in place
    (frozen forbids rebinding the attribute, not mutating the dict)."""

    mtime: float
    content_sha256: str
    full: bool
    rendered: dict[tuple[int, int], tuple[int, int]] = field(default_factory=dict)


@dataclass
class ToolContext:
    project_root: Path
    events_path: Path
    # Subagent dispatch fills these:
    subagent_defs: dict[str, AgentDef] = field(default_factory=dict)
    provider_name: str = ""
    model_name: str = ""
    parent_transcript_dir: Path | None = None
    running_processes: list[Any] = field(default_factory=list)
    # Background processes (bg.py): id -> BgProc, invocation-scoped. Registered
    # ONLY here — never on running_processes: that registry is drained by
    # terminate_running_processes on interrupt with the caller emitting
    # tool_killed from the result; routing a background proc through it would
    # produce the wrong kill_reason, no tombstone, and a spurious tool_killed.
    background_processes: dict[str, Any] = field(default_factory=dict)
    # Per-step read-before-write gate: resolved path -> last read record. A fresh
    # ToolContext is built per agent run, so the map is naturally per-step.
    read_state: dict[Path, ReadRecord] = field(default_factory=dict)
    # Background-execution opt-in. Only providers that wire the FULL bg
    # surface (bash_output/bash_kill injection, _sweep_bg sites, teardown in
    # interrupt + run()-finally) may set this — all four current providers
    # do. The default-False gate is future-provider defense: a new provider
    # exposing registry bash without that wiring would otherwise hand the
    # agent a process it can never read, stop, or sweep, leaking past the
    # invocation (the trap the atomic capability-set rationale in
    # _build_tools names).
    background_enabled: bool = False
    _bg_counter: int = 0
    # The change being processed; used by subagent dispatch to propagate slug/title.
    change_context: Any = None
    # Live State of the running Change; used by the subagent-authority guard
    # (rename_change raises if this is None). None for subagents (no rename authority).
    change_state: Any = None
    # The LIVE, mutable Change (top-level steps only; None for subagents). The
    # rename_change tool mutates change.title + rewrites change.json through it.
    # Distinct from change_context, which is the FROZEN render-time snapshot.
    change: Any = None
    change_home: Path | None = None
    # The runner injects this callable as the bare-turn-end / AWAITING_USER
    # resume input channel, used to read the user's reply. None for subagents.
    user_input_fn: Any = None
    # Shared EventLog WRITE instance, set by the parent runner at the same site
    # it sets spawn_subagent. dispatch_task threads this into the subagent's
    # AgentInvocation so subagent audit events land in the parent's log + fire
    # the parent's on_event. Distinct from the read-only EventLog dispatch_task
    # builds for prompt-context rendering.
    event_log: Any = None
    # The agent_id of THIS invocation (e.g. "step:review/final-review" or a
    # subagent id). Set by each provider where it builds the ToolContext, so an
    # event emitted for this invocation pairs under the right node.
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
    # Resolved tiered gate commands+timeouts (TestGates). Threaded onto the
    # subprocess env (IMP_TEST_CMD_*) by the bash tool. None => no gate vars.
    test_gates: TestGates | None = None
    # Tier A proactive-handoff watch threshold, propagated parent -> subagent via
    # this ctx so dispatch_task seeds each subagent's checkpoint_threshold.
    checkpoint_threshold: float | int | None = None
    # Resolved SandboxPolicy for shell wrapping (spec Part B). None => bare sh -c
    # (no sandbox), preserving pre-sandbox behavior. Set by the runner from
    # invocation.sandbox_policy.
    sandbox_policy: Any = None
    # §10/§12: when True, the run is headless (no human at the keyboard). The
    # subagent backoff loop applies a wall-clock ceiling instead of retrying
    # forever. Copied from invocation.headless by each provider.
    non_interactive: bool = False
    # Per-invocation task-dispatch width cap (design Decision 5). Lazily created
    # by get_parallel_limiter on first task dispatch; bounds one level's breadth.
    # NOT threaded into child invocations: each spawn builds a fresh ToolContext
    # with its own limiter (the nested-pool deadlock guard).
    parallel_limiter: Any = None
    # invocation_id counter for disambiguating same-named subagent invocations
    _subagent_counter: int = 0

    def next_subagent_invocation_id(self) -> int:
        """1, 2, 3, ... — per-invocation. subagents.py seeds _subagent_counter
        from parent_transcript_dir/subagents on resume so a post-resume dispatch
        never re-targets a persisted subagent dir (see
        subagents._seed_subagent_counter)."""
        self._subagent_counter += 1
        return self._subagent_counter

    def get_parallel_limiter(self) -> Any:
        """Lazily create THIS invocation's CapacityLimiter via a synchronous
        check-then-set (no await between — concurrent bridge handlers racing the
        create must not mint two limiters and double the cap). Width is the
        single resolved knob value; never inherited from a parent."""
        import anyio

        from imp.agents.parallel import max_parallel_tasks

        if self.parallel_limiter is None:
            self.parallel_limiter = anyio.CapacityLimiter(max_parallel_tasks())
        return self.parallel_limiter

    def next_bg_id(self) -> str:
        """"bg1", "bg2", ... — per-invocation. bg.py seeds _bg_counter from the
        scratch dir on resume so a post-resume spawn never shadows a persisted
        tombstone (see bg._seed_counter)."""
        self._bg_counter += 1
        return f"bg{self._bg_counter}"

    def resolve_path(self, path: str) -> Path:
        """Resolve an fs-tool path WITHOUT a worktree-containment raise.

        `(project_root / path.expanduser()).resolve()`: a relative path maps under
        project_root; a `~`-home path expands to an absolute host path, and any
        absolute path replaces the root (pathlib join semantics) and resolves to
        its literal host location. Out-of-worktree access is then bounded by
        authorize_fs (bounded by the sandbox mirror (allow-all when unsandboxed)),
        matching `bash`.
        """
        return (self.project_root / Path(path).expanduser()).resolve()

    def authorize_fs(
        self, ops: tuple[Literal["read", "write"], ...], path: str,
    ) -> Path:
        """The single guard every fs tool funnels through. Resolve (loose), then
        authorize each op against the sandbox policy — the same boundary bash
        obeys. An unsandboxed run is fully trusted (allow-all, bash parity); under
        an active policy each op is bounded by allows_read/allows_write. Raises
        PermissionDenied on a denied op; returns the resolved Path."""
        resolved = self.resolve_path(path)
        policy = self.sandbox_policy
        if policy is None or not policy.active:
            return resolved
        for op in ops:
            allowed = (
                policy.allows_read(resolved) if op == "read"
                else policy.allows_write(resolved)
            )
            if not allowed:
                raise PermissionDenied(
                    f"{op} on {resolved} denied by the sandbox policy"
                )
        return resolved


@contextlib.contextmanager
def suspend_inactivity_watchdog(ctx: ToolContext | None) -> Iterator[None]:
    coord = getattr(ctx, "coordinator", None)
    if coord is not None:
        coord.begin_tool_watchdog()
    try:
        yield
    finally:
        if coord is not None:
            coord.end_tool_watchdog()


async def request_bracketed(
    coordinator: Any, aiter: Any
) -> AsyncIterator[Any]:
    """Yield items from `aiter` while holding the coordinator's
    provider-request watchdog state across each await for the NEXT item — and
    only across that await, never across the yield.

    begin_request_watchdog() is called just before each `await it.__anext__()`,
    so while imp is blocked awaiting the provider's next response event the
    finite request-in-flight ceiling applies (see agent_run._bump). The instant
    the item arrives (or the stream ends, errors, or is cancelled) the finally
    calls end_request_watchdog(), releasing the state BEFORE the item is yielded
    — so consumer-side processing of each item runs under the normal window.
    Because depth is never held across a yield, no request depth leaks if the
    consumer breaks or raises mid-iteration, and cleanup is deterministic
    (try/finally) rather than dependent on async-generator finalization. A None coordinator
    — or one lacking the begin/end request-watchdog methods — degrades to a plain pass-through.
    """
    begin = getattr(coordinator, "begin_request_watchdog", None)
    end = getattr(coordinator, "end_request_watchdog", None)
    it = aiter.__aiter__()
    while True:
        if begin is not None:
            begin()
        try:
            item = await it.__anext__()
        except StopAsyncIteration:
            return
        finally:
            if end is not None:
                end()
        yield item


def change_home_path(change_home: str | None) -> Path | None:
    return Path(change_home) if change_home else None


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


def _parse_ppid(status_text: str) -> int | None:
    for line in status_text.splitlines():
        if line.startswith("PPid:"):
            with contextlib.suppress(ValueError):
                return int(line.split(":", 1)[1].strip())
            return None
    return None


def descendant_pids(root_pid: int) -> list[int]:
    """Transitive descendants of `root_pid`, enumerated from /proc PPid links
    (excludes root_pid itself). Returns [] where /proc is absent (non-Linux) — the
    caller then degrades to killpg-only. Gather this WHILE the parent tree is alive:
    once the parent dies its children reparent to init and the PPid links are gone.
    """
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return []
    children: dict[int, list[int]] = {}
    for entry in proc_root.iterdir():
        name = entry.name
        if not name.isdigit():
            continue
        try:
            status = (entry / "status").read_text()
        except OSError:
            continue                       # process vanished mid-scan
        ppid = _parse_ppid(status)
        if ppid is not None:
            children.setdefault(ppid, []).append(int(name))
    out: list[int] = []
    seen: set[int] = set()
    queue = list(children.get(root_pid, []))
    while queue:
        pid = queue.pop()
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        queue.extend(children.get(pid, []))
    return out


def _signal_pids(pids: Iterable[int], sig: int) -> None:
    for pid in pids:
        with contextlib.suppress(OSError):
            os.kill(pid, sig)


async def kill_process_tree(proc: Any, sig: int) -> None:
    """killpg the process's group AND signal its transitive (possibly re-grouped)
    descendants by pid. Enumerate descendants BEFORE the killpg, so a SIGKILL that
    severs the ppid links can't hide a re-grouped escapee. Single-shot — used by
    the in-tool cap/timeout kills; the SIGTERM->grace->SIGKILL reaping path uses the
    two-stage capture in terminate_running_processes."""
    pid = getattr(proc, "pid", None)
    descendants = descendant_pids(pid) if pid is not None else []
    await kill_process_group(proc, sig)
    _signal_pids(descendants, sig)


async def terminate_running_processes(
    ctx: ToolContext, *, timeout: float = PROCESS_KILL_GRACE_S,
) -> list[Any]:
    """SIGTERM the process GROUP of every registered process AND its transitive
    descendants, wait `timeout` (named grace, default 4 s), then SIGKILL the group
    + descendants of any survivor. Drain the registry. Returns the processes that
    were signalled (caller emits tool_killed from it). Escalation is TIME-gated,
    not count-gated. Descendants that left the group (setpgid/setsid) are reached
    by pid via a /proc walk, captured while the parent tree is still alive."""
    import anyio

    if not ctx.running_processes:
        return []
    procs = list(ctx.running_processes)
    ctx.running_processes.clear()

    captured: dict[int, set[int]] = {}
    for p in procs:
        pid = getattr(p, "pid", None)
        ds = set(descendant_pids(pid)) if pid is not None else set()
        captured[id(p)] = ds
        await kill_process_group(p, _signal.SIGTERM)
        _signal_pids(ds, _signal.SIGTERM)

    async def _wait_one(p: Any) -> None:
        with contextlib.suppress(Exception):
            await p.wait()

    with anyio.move_on_after(timeout):
        async with anyio.create_task_group() as tg:
            for p in procs:
                tg.start_soon(_wait_one, p)

    for p in procs:
        pid = getattr(p, "pid", None)
        # Re-enumerate BEFORE the group SIGKILL severs the ppid links, then union
        # with the SIGTERM-stage capture so an escapee whose in-group parent already
        # died during grace (now reparented to init) is still reached by pid.
        fresh = set(descendant_pids(pid)) if pid is not None else set()
        survivors = captured[id(p)] | fresh
        if getattr(p, "returncode", None) is None:
            await kill_process_group(p, _signal.SIGKILL)
            # Bound the reap like bg.py: a D-state child could block here forever.
            with anyio.move_on_after(timeout):
                with contextlib.suppress(Exception):
                    await p.wait()
        _signal_pids(survivors, _signal.SIGKILL)
    return procs
