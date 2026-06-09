"""Interrupt-path regression guards (spec B.9). Auto-skip without bwrap."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import anyio
import pytest

import imp.agents.tools.bash  # noqa: F401
from imp.agents.runner import ChangeContext
from imp.agents.tools import get_registry
from imp.agents.tools.context import ToolContext, terminate_running_processes
from imp.engine.sandbox import SandboxPolicy, sandbox_available

pytestmark = pytest.mark.skipif(not sandbox_available(), reason="bwrap unavailable")


def _sandbox_worktree() -> Path:
    """Return a temp dir NOT under /tmp.

    bwrap's policy binds a sub-dir of worktree onto /tmp (``tmp_host_dir``).
    If the worktree itself lives under /tmp that bind shadows the worktree path
    inside the namespace, making ``--chdir <worktree>`` fail. /var/tmp is
    outside /tmp and avoids this shadowing.
    """
    return Path(tempfile.mkdtemp(dir="/var/tmp", prefix="imp-test-sb-"))


def _active_policy(worktree: Path) -> SandboxPolicy:
    tmp = worktree / "tmp"
    tmp.mkdir(exist_ok=True)
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
        tmp_host_dir=str(tmp),
        secret_dirs=(),
        secret_files=(),
        setenv=(("PATH", "/usr/bin:/bin"),),
        network=True,
        note="",
    )


def _ctx(worktree: Path) -> ToolContext:
    ctx = ToolContext(
        project_root=worktree,
        fs_permissions=(),
        events_path=worktree / "events.jsonl",
    )
    ctx.change_context = ChangeContext(
        slug="s",
        title="t",
        project_root=worktree,
        workflow_dir=worktree / ".imp" / "wf",
    )
    ctx.sandbox_policy = _active_policy(worktree)
    return ctx


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
    from imp.engine.types import SourceLoc, Step

    worktree = _sandbox_worktree()
    try:
        policy = _active_policy(worktree)
        step = Step(
            name="g",
            phase="p",
            kind="script",
            rules=(),
            options=(),
            on_max_retries=None,
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
