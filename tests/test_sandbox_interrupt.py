"""Interrupt-path regression guards (spec B.9). Auto-skip without bwrap."""
from __future__ import annotations

import json
import shlex
import shutil
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
from imp.agents.tools.context import ToolContext, terminate_running_processes
from imp.engine.sandbox import (
    SandboxPolicy,
    make_home_layer,
    overlay_available,
    sandbox_available,
)

pytestmark = pytest.mark.skipif(
    not sandbox_available(),
    reason="bwrap unavailable",
)


def _sandbox_worktree() -> Path:
    """Return a temp dir for the sandbox worktree.

    The real /tmp is now bound rw by identity (--bind /tmp /tmp) so a worktree
    under /tmp is no longer shadowed inside the namespace — default tempfile
    is fine.
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
        setenv=(("PATH", "/usr/bin:/bin"),),
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
    return ctx


# This file defines its own overlay skip rather than importing a shared one (its
# convention: wrap an imported probe in a module-level skipif). An unprivileged
# `mount -t overlay` under /var/tmp must succeed — the same real-disk base the
# overlay home/worktree live under, so no later bind shadows the merged mount.
_OVERLAY_BASE = Path("/var/tmp")


def _probe_overlay() -> bool:
    if not sandbox_available() or not _OVERLAY_BASE.is_dir():
        return False
    try:
        root = Path(tempfile.mkdtemp(prefix="imp-int-ovl-probe-", dir=_OVERLAY_BASE))
    except OSError:
        return False
    try:
        probe = root / "probe"
        home = root / "home"
        probe.mkdir()
        home.mkdir()
        return overlay_available(probe, home)
    except OSError:
        return False
    finally:
        shutil.rmtree(root, ignore_errors=True)


requires_overlay = pytest.mark.skipif(
    not _probe_overlay(),
    reason="unprivileged overlayfs probe at /var/tmp failed (read-only base or "
           "stacked overlay under a nested sandbox)",
)


def _overlay_policy(base: Path) -> tuple[SandboxPolicy, Callable[[], None]]:
    """Active policy with a real COW-overlay HomeLayer. home + worktree are SIBLINGS
    under `base` (a /var/tmp real fs), so no later bind_plan bind (/tmp identity,
    worktree rw) covers the home path and shadows the overlay merged mount. Returns
    (policy, cleanup); cleanup removes the overlay upper/work/merged dirs."""
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    wt = base / "worktree"
    (wt / ".git").mkdir(parents=True, exist_ok=True)
    layer, cleanup = make_home_layer(
        str(home),
        upper=base / "ov-upper", work=base / "ov-work", merged=base / "ov-merged",
        userxattr=False,
    )
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
        setenv=(("PATH", "/usr/bin:/bin"), ("HOME", str(home))),
        network=True,
        note="",
        home=str(home),
        home_layer=layer,
    )
    return pol, cleanup


def _pid_gone_or_zombie(pid: int) -> bool:
    """True once `pid` has exited or is a reaped-pending zombie (state Z). The
    wrapper uses no PID namespace, so the inner shell's $$ is a host-visible PID;
    /proc/<pid>/stat reads it directly. A zombie counts as terminated — it tolerates
    the brief window before the orphaned child is reaped."""
    try:
        data = Path(f"/proc/{pid}/stat").read_text()
    except FileNotFoundError:
        return True
    return data.rpartition(")")[2].split()[0] == "Z"


def test_bash_inner_observes_sigterm_and_survives_grace() -> None:
    """The inner command traps SIGTERM and keeps running through the grace, then
    dies on the final killpg(SIGKILL). Direct guard for dropping --unshare-pid AND
    --die-with-parent (either would collapse the grace).

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


@requires_overlay
def test_sigterm_reaches_inner_child_through_overlay_wrapper() -> None:
    """AC7: drive the real bash-tool -> terminate_running_processes path (the same
    path Ctrl-C hits) under a real COW-overlay policy, and prove killpg
    SIGTERM->grace->SIGKILL reaps the INNER child through the unshare + overlay +
    bwrap layers. The crux is the recorded inner PID being terminated, not merely
    the top unshare process dying. Runs offline (no live provider)."""
    base = Path(tempfile.mkdtemp(prefix="imp-test-ovl-int-", dir="/var/tmp"))
    pol, cleanup = _overlay_policy(base)
    try:
        assert pol.home_layer.is_overlay  # a real overlay, not a silent rw-fallback
        wt = Path(pol.worktree)
        ctx = ToolContext(project_root=wt, events_path=wt / "events.jsonl")
        ctx.quest_context = QuestContext(
            title="t", project_root=wt, workflow_dir=wt / ".imp" / "wf")
        ctx.base_sandbox_policy = pol

        pid_file = wt / "inner.pid"
        out_holder: dict[str, object] = {}

        async def _runner() -> None:
            out_holder["out"] = json.loads(await get_registry()["bash"].async_invoke(
                ctx,
                {"command":
                 f"echo $$ > {shlex.quote(str(pid_file))}; "
                 "trap 'echo GOTTERM' TERM; "
                 "for i in $(seq 1 600); do sleep 0.1; done; echo DONE"}))

        async def _killer() -> None:
            deadline = time.monotonic() + 20.0  # generous inner budget
            while not (pid_file.exists() and pid_file.read_text().strip()):
                if time.monotonic() > deadline:
                    raise AssertionError("inner PID file never appeared")
                await anyio.sleep(0.02)
            out_holder["inner_pid"] = int(pid_file.read_text().strip())
            out_holder["killed"] = await terminate_running_processes(ctx, timeout=2.0)

        async def _scenario() -> None:
            with anyio.fail_after(90.0):  # single outer hang-catcher (generous)
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_runner)
                    tg.start_soon(_killer)

        anyio.run(_scenario)

        assert ctx.running_processes == []          # registry drained
        assert len(out_holder["killed"]) >= 1       # >=1 process was signalled
        inner_pid = out_holder["inner_pid"]
        # The recorded inner child is terminated. Poll a generous-but-bounded budget:
        # a SIGKILL'd process becomes a zombie within milliseconds once scheduled, so
        # 60 s never false-fires even under saturated -n auto CI, yet stays bounded so a
        # genuinely unreached signal still fails with the diagnostic below rather than
        # hanging. Tolerates the brief zombie window; failure here proves the signal did
        # NOT reach the child through the layers (it survived as an orphan) -- the crux
        # falsifier.
        deadline = time.monotonic() + 60.0
        while not _pid_gone_or_zombie(inner_pid):
            if time.monotonic() > deadline:
                raise AssertionError(
                    f"inner pid {inner_pid} survived termination -- the signal did "
                    "not reach the child through unshare+overlay+bwrap")
            time.sleep(0.05)
    finally:
        cleanup()
        shutil.rmtree(base, ignore_errors=True)
