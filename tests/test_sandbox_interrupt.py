"""Interrupt-path regression guards (spec B.9). Auto-skip without bwrap."""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.runner import QuestContext
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext, descendant_pids, terminate_running_processes
from imp.engine.sandbox import (
    SandboxPolicy,
    make_home_layer,
    sandbox_available,
)
from imp.engine.types import Pause, SourceLoc, Step
from tests.conftest import (
    _pid_gone_or_zombie,
    assert_all_tool_contexts_owned,
    own_tool_context,
)
from tests.overlay_probe import OVERLAY_MODE, requires_overlay

pytestmark = pytest.mark.skipif(
    not sandbox_available(),
    reason="bwrap unavailable",
)


def _sandbox_worktree() -> Path:
    """Return a temp dir for the sandbox worktree.

    The worktree gets its own rw bind (emitted after the per-quest scratch bind),
    so a worktree under the default temp dir is not shadowed inside the namespace
    — default tempfile is fine.
    """
    return Path(tempfile.mkdtemp(prefix="imp-test-sb-"))


def _active_policy(worktree: Path) -> SandboxPolicy:
    git = worktree / ".git"
    git.mkdir(exist_ok=True)
    return SandboxPolicy(
        active=True,
        worktree=str(worktree),
        git_common_dir=str(git),
        git_ro_remask=(),
        writable_caches=(),
        extra_readable=(),
        extra_writable=(),
        secret_dirs=(),
        secret_files=(),
        child_env=(("PATH", "/usr/bin:/bin"),),
        network=True,
        note="",
    )


def _ctx(worktree: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=worktree,
        events_path=worktree / "events.jsonl",
    )
    ctx.quest_context = QuestContext(
        title="t",
        project_root=worktree,
        workflow_dir=worktree / ".imp" / "wf",
    )
    ctx.base_sandbox_policy = _active_policy(worktree)
    return own_tool_context(ctx)


def _overlay_policy(base: Path) -> tuple[SandboxPolicy, Callable[[], None]]:
    """Active policy whose every spawn mints its own COW-overlay layer under a
    per-step overlay root. home + worktree are SIBLINGS under `base` (a /var/tmp
    real fs), so no later bind_plan bind (scratch dir, worktree rw) covers the
    home path and shadows the overlay merged mount. Returns (policy, cleanup);
    cleanup removes the whole overlay root."""
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    wt = base / "worktree"
    (wt / ".git").mkdir(parents=True, exist_ok=True)
    layer, overlay_root, cleanup = make_home_layer(
        str(home), root=base / "ovl", userxattr=OVERLAY_MODE)
    pol = SandboxPolicy(
        active=True,
        worktree=str(wt),
        git_common_dir=str(wt / ".git"),
        git_ro_remask=(),
        writable_caches=(),
        extra_readable=(),
        extra_writable=(),
        secret_dirs=(),
        secret_files=(),
        child_env=(("PATH", "/usr/bin:/bin"), ("HOME", str(home))),
        network=True,
        note="",
        home=str(home),
        home_layer=layer,
        overlay_root=overlay_root,
    )
    return pol, cleanup


def test_bash_inner_observes_sigterm_and_survives_grace() -> None:
    """The inner command traps SIGTERM and keeps running through the grace, then
    dies on the final killpg(SIGKILL). Direct guard against --die-with-parent, which
    would collapse the grace.

    Regression: if --die-with-parent were present, the inner sh would be
    instant-killed the moment bwrap exits on SIGTERM, collapsing the grace window.
    With the active policy (no --die-with-parent), terminate_running_processes
    must complete without hanging.
    """
    worktree = _sandbox_worktree()
    try:
        ctx = _ctx(worktree)
        assert ctx.sandbox_policy is not None
        assert ctx.sandbox_policy.active is True

        out_holder: dict[str, object] = {}

        async def _scenario() -> None:
            async with anyio.create_task_group() as tg:
                async def _runner() -> None:
                    result = await get_registry()["bash"].async_invoke(
                        ctx,
                        {"command": (
                            "trap 'echo GOTTERM' TERM; "
                            "for i in $(seq 1 50); do sleep 0.1; done; echo DONE"
                        )},
                    )
                    out_holder["out"] = json.loads(result)

                async def _killer() -> None:
                    # Wait for the trap to be installed, then interrupt.
                    await anyio.sleep(0.5)
                    killed = await terminate_running_processes(ctx, timeout=1.0)
                    out_holder["killed"] = killed

                tg.start_soon(_runner)
                tg.start_soon(_killer)

        t0 = time.monotonic()
        anyio.run(_scenario)
        elapsed = time.monotonic() - t0
    finally:
        shutil.rmtree(worktree, ignore_errors=True)

    # Must complete well within the loop's natural duration (5s) — not hang.
    assert elapsed < 10.0, f"scenario took {elapsed:.1f}s — possible orphan/hang"
    # Registry is drained.
    assert ctx.running_processes == []
    # At least one process was killed.
    assert len(out_holder["killed"]) >= 1  # type: ignore[arg-type]


def test_gate_timeout_kills_group_no_orphan() -> None:
    """A gate exceeding its timeout under bwrap is killed via killpg; the inner
    sleep is reaped (no orphan). Coupled gate-path change (M2/H-A).

    Regression: if --die-with-parent were absent AND killpg were not used, the
    inner `sleep 30` would be orphaned and the process group would outlive the
    bwrap pid, causing a 30s hang instead of a quick TimeoutExpired.
    """
    from imp.engine.gates import run_script
    from imp.engine.types import Pause, SourceLoc, Step

    worktree = _sandbox_worktree()
    try:
        policy = _active_policy(worktree)
        step = Step(
            name="g",
            phase="p",
            kind="script",
            rules=(),
            on_max_retries=Pause(),
            max_loops=1,
            source=SourceLoc(file=Path(__file__), line=1),
            command="sleep 30",
            timeout_seconds=1,
        )
        t0 = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_script(step, cwd=worktree, policy=policy)
        elapsed = time.monotonic() - t0
    finally:
        shutil.rmtree(worktree, ignore_errors=True)

    # Killed promptly (not hung 30s waiting on an orphaned inner sleep).
    assert elapsed < 10, f"gate took {elapsed:.1f}s — possible orphan/hang"


def _runs_the_command(pid: int, marker: str) -> bool:
    """The shell actually running the command, host-side. Its cmdline cannot name
    the command any more — that travels in $IMP_COMMAND, off argv — but the
    exec-time environment block still does, which is the same reason §2 calls this
    not-secrecy rather than confidentiality."""
    try:
        parts = Path(f"/proc/{pid}/cmdline").read_bytes().decode(
            errors="replace").split("\x00")
        environ = Path(f"/proc/{pid}/environ").read_bytes().decode(errors="replace")
    except OSError:
        return False
    return parts[:2] == ["sh", "-c"] and marker in environ


@requires_overlay
def test_sigterm_reaches_the_joined_shell_inside_the_keeper() -> None:
    """AC7: drive the real bash-tool → terminate_running_processes path (the same
    path Ctrl-C hits) under a real COW-overlay policy, and prove killpg
    SIGTERM→grace→SIGKILL reaps the processes INSIDE the keeper through the nsenter
    + unshare + bwrap layers. The crux is that the shell actually running the
    command dies, not merely the top unshare process: it is identified host-side by
    /proc/<pid>/cmdline among the descendants enumerated while the tree is still
    alive. An inner `$$` cannot serve — under a pid namespace it is a namespace-local
    pid that names an unrelated host process. Runs offline (no live provider)."""
    base = Path(tempfile.mkdtemp(prefix="imp-test-ovl-int-", dir="/var/tmp"))
    pol, cleanup = _overlay_policy(base)
    try:
        assert pol.overlay_root  # a real overlay root, not a silent rw-fallback
        wt = Path(pol.worktree)
        ctx = ToolContext(project_root=wt, events_path=wt / "events.jsonl")
        ctx.quest_context = QuestContext(
            title="t", project_root=wt, workflow_dir=wt / ".imp" / "wf")
        ctx.base_sandbox_policy = pol
        ctx = own_tool_context(ctx)

        ready = wt / "inner.up"
        out_holder: dict[str, object] = {}

        async def _runner() -> None:
            out_holder["out"] = json.loads(await get_registry()["bash"].async_invoke(
                ctx,
                {"command":
                 f"touch {shlex.quote(str(ready))}; "
                 "trap 'echo GOTTERM' TERM; "
                 "for i in $(seq 1 600); do sleep 0.1; done; echo DONE"}))

        async def _killer() -> None:
            deadline = time.monotonic() + 20.0  # generous inner budget
            while not ready.exists():
                if time.monotonic() > deadline:
                    raise AssertionError("the sandboxed command never signalled readiness")
                await anyio.sleep(0.02)
            procs = list(ctx.running_processes)
            assert procs, "the bash tool registered no process"
            # Enumerate WHILE the tree is alive: once the root dies the PPid links
            # are gone, and /proc/<pid>/cmdline with them.
            inner = descendant_pids(procs[0].pid)
            out_holder["inner_pids"] = inner
            out_holder["command_pids"] = [
                pid for pid in inner if _runs_the_command(pid, str(ready))]
            out_holder["killed"] = await terminate_running_processes(ctx, timeout=2.0)

        async def _scenario() -> None:
            with anyio.fail_after(90.0):  # single outer hang-catcher (generous)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_runner)
                    tg.start_soon(_killer)

        anyio.run(_scenario)

        assert ctx.running_processes == []          # registry drained
        assert len(out_holder["killed"]) >= 1       # >=1 process was signalled
        inner_pids = out_holder["inner_pids"]
        assert out_holder["command_pids"], (
            f"none of {inner_pids} was the sandboxed shell running the command")
        # Every process that was inside the sandbox is terminated. Poll a
        # generous-but-bounded budget: a SIGKILL'd process becomes a zombie within
        # milliseconds once scheduled, so 60 s never false-fires even under saturated
        # -n auto CI, yet stays bounded so a genuinely unreached signal still fails
        # with the diagnostic below rather than hanging. Failure here proves the
        # signal did NOT reach the children through the layers -- the crux falsifier.
        deadline = time.monotonic() + 60.0
        while True:
            alive = [pid for pid in inner_pids if not _pid_gone_or_zombie(pid)]
            if not alive:
                break
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"inner pids {alive} survived termination -- the signal did not "
                    "reach the children through unshare+overlay+bwrap")
            time.sleep(0.05)
    finally:
        cleanup()
        shutil.rmtree(base, ignore_errors=True)


def _delay_kill_until_ready(monkeypatch: pytest.MonkeyPatch, ready: Path) -> list[int]:
    """Hold the gate's teardown until the sandboxed command has signalled readiness,
    so bwrap startup latency under load cannot race the SIGTERM. Returns the list
    that the wrapper fills with the live host-side tree (bwrap plus everything under
    it) as of that instant — the only moment it can be enumerated, since run_command
    owns `proc`. The real helper then runs unchanged."""
    from imp.engine import gates

    real_kill = gates._graceful_kill
    recorded: list[int] = []

    def _delayed(proc: subprocess.Popen[bytes]) -> None:
        deadline = time.monotonic() + 20.0
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        recorded.append(proc.pid)
        recorded.extend(descendant_pids(proc.pid))
        real_kill(proc)

    monkeypatch.setattr(gates, "_graceful_kill", _delayed)
    return recorded


def _gate_step(command: str) -> Step:
    return Step(
        name="g", phase="p", kind="script", rules=(), on_max_retries=Pause(),
        max_loops=1, source=SourceLoc(file=Path(__file__), line=1),
        command=command, timeout_seconds=1,
    )


def test_gate_teardown_leaves_no_survivors_when_command_ignores_sigterm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A TERM-ignoring gate command under an active sandbox policy leaves ZERO live
    processes behind. The host-side tree is enumerated before the kill; every pid
    must end up gone or a zombie. Regression for the escalation leak: without the
    group-liveness poll the outer bwrap satisfies the wait at ~0s, the SIGKILL is
    skipped, and the inner shell loops on forever as an orphan."""
    from imp.engine import gates

    worktree = _sandbox_worktree()
    ready = worktree / "up"
    recorded = _delay_kill_until_ready(monkeypatch, ready)
    try:
        step = _gate_step(
            f"trap '' TERM; touch {shlex.quote(str(ready))}; "
            "while :; do sleep 0.2; done")
        with pytest.raises(subprocess.TimeoutExpired):
            gates.run_script(step, cwd=worktree, policy=_active_policy(worktree))
    finally:
        shutil.rmtree(worktree, ignore_errors=True)

    assert len(recorded) >= 2, "the sandboxed process tree was never observed"
    deadline = time.monotonic() + 20.0
    while True:
        alive = [pid for pid in recorded if not _pid_gone_or_zombie(pid)]
        if not alive:
            break
        if time.monotonic() > deadline:
            for pid in alive:                     # don't leak the runaway tree on failure
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
            pytest.fail(f"pids {alive} survived gate teardown — SIGKILL escalation skipped")
        time.sleep(0.05)


def test_gate_teardown_lets_sigterm_trap_finish_before_sigkill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The grace window is a real window for the sandboxed children. The command's
    SIGTERM trap sleeps 0.3s and only then writes a marker into the worktree,
    host-visible through the rw bind; the marker must exist once TimeoutExpired has
    been raised. This is the falsifier for the escalation shape: keying on proc's
    exit lands the SIGKILL milliseconds after the SIGTERM — the outer bwrap exits at
    ~0s — so the trap never reaches its write. Nothing here asserts elapsed time;
    the marker is polled."""
    from imp.engine import gates

    # The grace is widened from its real 2.0s so the trap's 0.3s has ~25x headroom
    # instead of ~6x: the only fixed duration in this test must not race a saturated
    # -n auto run. Costs nothing on the passing path (teardown short-circuits the
    # moment the trap exits and the group empties) and does not weaken Step 4's
    # falsification, where the SIGKILL lands within ms of the SIGTERM regardless.
    monkeypatch.setattr(gates, "GATE_KILL_GRACE_S", 8.0)

    worktree = _sandbox_worktree()
    ready = worktree / "up"
    marker = worktree / "trap-finished"
    # The readiness delay is what this test needs from the wrapper; the pid list it
    # returns is the sibling test's concern.
    _delay_kill_until_ready(monkeypatch, ready)
    try:
        step = _gate_step(
            f"trap 'sleep 0.3; touch {shlex.quote(str(marker))}; exit 0' TERM; "
            f"touch {shlex.quote(str(ready))}; "
            "while :; do sleep 0.2; done")
        with pytest.raises(subprocess.TimeoutExpired):
            gates.run_script(step, cwd=worktree, policy=_active_policy(worktree))

        deadline = time.monotonic() + 20.0
        while not marker.exists():
            if time.monotonic() > deadline:
                pytest.fail("the SIGTERM trap never completed — the group SIGKILL "
                            "landed before the grace window could be used")
            time.sleep(0.02)
    finally:
        shutil.rmtree(worktree, ignore_errors=True)


def test_every_tool_context_here_is_owned():
    assert_all_tool_contexts_owned(Path(__file__))
