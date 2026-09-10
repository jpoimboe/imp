"""The keeper namespace hung off a ToolContext: lazy open, reuse, rebuild, and the
cross-call guarantees an agent run sees.

Real keepers driven through the real bash tool, asserting the OS-level outcome.
Helpers come from tests.test_sandbox_namespace so the two files cannot drift.
"""
from __future__ import annotations

import contextlib
import dataclasses
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from imp.agents.runner import QuestContext
from imp.agents.tools import get_registry
from imp.agents.tools.bg import _tombstone_path
from imp.agents.tools.context import (
    ToolContext,
    ensure_tool_namespace,
    teardown_tool_context,
    terminate_tool_processes,
)
from imp.engine.sandbox import (
    SANDBOX_ENTERED_MARKER,
    SandboxNamespace,
    sandbox_available,
)
from tests.conftest import own_tool_context
from tests.test_sandbox_namespace import _ALIVE, _host_pids, _make_root, _policy_for, _poll

# The module timeout is the SINGLE outer backstop for a genuine hang; it exceeds
# the sum of the inner budgets any one test can spend.
pytestmark = [
    pytest.mark.skipif(not sandbox_available(), reason="bwrap unavailable"),
    pytest.mark.timeout(300),
]


def _ctx(root: Path) -> ToolContext:
    ctx = ToolContext(project_root=root, events_path=root / "events.jsonl")
    ctx.quest_context = QuestContext(
        id="test01", title="Test Quest", project_root=root,
        workflow_dir=root / ".imp" / "wf")
    ctx.base_sandbox_policy = _policy_for(root)
    # A real coordinator's only surface here: bash reads should_pause every 50ms
    # and ctx.sandbox_policy reads unsandboxed on every access (the /sandbox toggle).
    ctx.coordinator = SimpleNamespace(unsandboxed=False, should_pause=False)
    ctx.agent_id = "step:build/coder"
    return own_tool_context(ctx)


def _open(ctx: ToolContext):
    return anyio.run(lambda: ensure_tool_namespace(
        ctx, engine_env={}, on_retry=lambda _n, _d: None))


def _bash(ctx: ToolContext, command: str, **args: object) -> dict:
    return json.loads(anyio.run(
        get_registry()["bash"].async_invoke, ctx, {"command": command, **args}))


def test_the_namespace_opens_once_and_is_reused():
    # Lazy open on first use, then reuse: a second call must not spawn a second
    # keeper, or every call would pay the spawn AND lose the previous call's
    # processes.
    root = _make_root()
    try:
        ctx = _ctx(root)
        assert ctx.sandbox_ns is None            # nothing paid before first use
        first = _open(ctx)
        assert _open(ctx) is first
        assert ctx.sandbox_ns is first
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_changed_base_policy_rebuilds_the_namespace():
    # create_and_enter_worktree reassigns ctx.base_sandbox_policy mid-run
    # (create_and_enter_worktree.py:126-131), and a keeper pinned to the
    # pre-materialize bind plan could not see the worktree the agent was just told
    # it is in. Keyed on the value, so no caller has to remember a hook.
    root = _make_root()
    other = _make_root()
    try:
        ctx = _ctx(root)
        first = _open(ctx)
        ctx.base_sandbox_policy = dataclasses.replace(
            ctx.base_sandbox_policy, worktree=str(other))
        second = _open(ctx)
        assert second is not first
        assert first.closed                      # the old one is closed, not leaked
        assert second.base_policy is ctx.base_sandbox_policy
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(other, ignore_errors=True)


def test_an_unchanged_policy_value_is_not_a_rebuild():
    # The key is the VALUE, not the identity: rebuilding on an equal-but-new
    # object would kill the agent's own background work for nothing.
    root = _make_root()
    try:
        ctx = _ctx(root)
        first = _open(ctx)
        ctx.base_sandbox_policy = dataclasses.replace(ctx.base_sandbox_policy)
        assert _open(ctx) is first
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_the_keeper_survives_the_run_loop_that_opened_it():
    # --die-with-parent is PR_SET_PDEATHSIG, which the kernel fires when the
    # CREATING THREAD exits. Opening in a worker thread therefore kills the keeper
    # the moment that worker retires; this pins that open() runs on the caller's
    # own thread, across the anyio.run boundary every tool call crosses.
    root = _make_root()
    try:
        ctx = _ctx(root)
        ns = _open(ctx)                          # one anyio.run, now finished
        anyio.run(anyio.sleep, 0.01)             # and another, opening nothing
        spawn = ns.join("echo alive", cwd=str(root), engine_env={})
        done = subprocess.run(spawn.argv, env=spawn.env, pass_fds=spawn.pass_fds,
                              capture_output=True, text=True, timeout=30.0)
        assert done.returncode == 0, done.stderr
        assert done.stdout.strip() == "alive"
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_process_left_running_is_visible_and_killable_in_a_later_call():
    # AC 1, and the incident's fix: today a pid recorded in one call is meaningless
    # in the next, so the cleanup an agent attempts could not have worked.
    root = _make_root()
    marker = f"imp-run-{uuid.uuid4().hex}"
    try:
        ctx = _ctx(root)
        a = _bash(ctx, f"nohup setsid sh -c '{_ALIVE}' {marker} >/dev/null 2>&1 &")
        assert a["returncode"] == 0, f"call A failed: {a}"
        # AC 9b, healthy path: the sentinel is consumed by the keeper's handshake
        # and never reaches a call's output.
        assert SANDBOX_ENTERED_MARKER not in a["stderr"]
        _poll(lambda: _host_pids(marker), what=f"{marker} alive host-side")

        b = _bash(ctx, f"pgrep -f {marker}")
        assert b["returncode"] == 0, f"call B could not see call A's process: {b}"
        ns_pid = b["stdout"].split()[0]

        c = _bash(ctx, f"kill {ns_pid}")
        assert c["returncode"] == 0, f"call B could not kill it: {c}"
        _poll(lambda: not _host_pids(marker),
              what=f"{marker} gone after the later call killed it")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_teardown_kills_every_survivor_shape_after_the_background_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 2. (a) four survivor shapes, zero after teardown, verified host-side on a
    # marker that appears on no other process's argv. (b) the background
    # escalation COMPLETED before the namespace close — observed as ORDER, which
    # is what the AC asks for ("pinned by observing teardown order rather than
    # wall-clock timing"). The observable is the tombstone file, which
    # terminate_background_processes writes unconditionally and synchronously in
    # the tombstone read-merge-write step, after its SIGKILL pass. Its existence
    # at close time therefore proves the escalation ran to completion before the
    # namespace was closed. bp.status alone would be weaker — it flips in the
    # pre-SIGTERM check-and-set pass, so it proves the escalation STARTED, not
    # that it finished. No timing is involved in either direction.
    #
    # Deliberately NOT pinned by a SIGTERM trap racing to write a marker. The
    # background grace keys only on bp.proc.wait() (bg.py:538-541 — no
    # group-liveness poll, unlike terminate_running_processes at context.py:407),
    # and under a join the leader is nsenter, which has no TERM handler and dies at
    # ~0s. The "4s grace" therefore collapses to the ~2ms Python needs to reap the
    # leader, walk /proc and killpg, and any trap must beat that. Measured through
    # this exact path: a `touch` trap lost 1/25 on an idle box, and a `: >` builtin
    # lost 1/25 under CPU load — a coin-flip either way, on the axis this project
    # is least willing to spend.
    root = _make_root()
    marker = f"imp-run-{uuid.uuid4().hex}"
    plant = (
        f"sh -c '{_ALIVE}' {marker}-plain >/dev/null 2>&1 &\n"
        f"nohup setsid sh -c '{_ALIVE}' {marker}-setsid >/dev/null 2>&1 &\n"
        f"( ( sh -c '{_ALIVE}' {marker}-double >/dev/null 2>&1 & ) & )\n"
        f"""sh -c "trap '' TERM; {_ALIVE}" {marker}-trap >/dev/null 2>&1 &\n"""
    )
    try:
        ctx = _ctx(root)
        ctx.background_enabled = True
        _bash(ctx, f"sh -c '{_ALIVE}' {marker}-bg", run_in_background=True)
        assert _bash(ctx, plant)["returncode"] == 0
        # five: the four planted shapes plus the background command itself, which
        # must be reaped too — through its own escalation, not by the atomic kill.
        _poll(lambda: len(_host_pids(marker)) == 5,
              what=f"five {marker} processes alive host-side")

        # SandboxNamespace is a slots dataclass, so the recorder patches the CLASS;
        # monkeypatch restores it at teardown.
        observed: dict[str, bool] = {}
        real_close = SandboxNamespace.close

        def _recording_close(self: SandboxNamespace) -> None:
            if "tombstoned_at_close" not in observed:
                # the entry's returncode is not checked here: `await bp.proc.wait()` inside
                # terminate_background_processes can't update the entry's proc.returncode
                # across event-loop boundaries (each anyio.run() creates a new
                # asyncio loop; the child watcher for the spawning loop is closed).
                # The tombstone file IS synchronous stdlib I/O, written
                # unconditionally AFTER the SIGKILL pass — its existence proves the
                # escalation ran to completion before close() was called.
                observed["tombstoned_at_close"] = _tombstone_path(ctx).exists()
            real_close(self)

        monkeypatch.setattr(SandboxNamespace, "close", _recording_close)

        anyio.run(teardown_tool_context, ctx)

        _poll(lambda: not _host_pids(marker),
              what=f"zero {marker} survivors after teardown")
        assert observed["tombstoned_at_close"], (
            "the namespace was closed before the background tombstone was written")
        assert ctx.sandbox_ns is None
    finally:
        shutil.rmtree(root, ignore_errors=True)


_DEADMAN = """
import os, subprocess, sys, time
from pathlib import Path
from imp.engine.sandbox import SandboxNamespace, SandboxPolicy
root = Path(sys.argv[1])
marker = os.environ["IMP_KEEPER_MARKER"]     # env, never argv: see the test below
(root / ".git").mkdir(exist_ok=True)
policy = SandboxPolicy(
    active=True, worktree=str(root), git_common_dir=str(root / ".git"),
    git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
    secret_dirs=(), secret_files=(),
    child_env=(("PATH", os.environ["PATH"]), ("HOME", str(root))),
    network=True, note="")
ns = SandboxNamespace.open(policy=policy, engine_env={}, environ=os.environ)
spawn = ns.join("nohup setsid sh -c 'while :; do sleep 5; done' " + marker
                + " >/dev/null 2>&1 &", cwd=str(root), engine_env={})
subprocess.run(spawn.argv, env=spawn.env, pass_fds=spawn.pass_fds, check=True)
while True:
    time.sleep(1)
"""


def test_killing_imp_takes_the_namespace_with_it():
    # AC 3, the last hole: if imp dies abnormally — crash, OOM, SIGKILL — nothing
    # runs a teardown, so --die-with-parent on the keeper has to be what reaps it.
    #
    # The marker reaches the driver in its ENVIRONMENT and only ever lands on the
    # in-namespace process's argv. That is what makes this falsifiable: pgrep -f
    # matches argv, so a marker passed as sys.argv[2] would match the DRIVER, the
    # readiness poll would fire during interpreter startup (measured: 1.9ms, before
    # `import imp.engine.sandbox` finishes) and the closing poll would be satisfied
    # by the driver's own death — passing with the flag deleted.
    root = _make_root()
    marker = f"imp-run-{uuid.uuid4().hex}"
    # No pipe: readiness is polled host-side on the marker, so a PIPE nothing
    # reads would only risk wedging the child on a full buffer.
    child = subprocess.Popen(
        [sys.executable, "-c", _DEADMAN, str(root)],
        env={**os.environ, "IMP_KEEPER_MARKER": marker})
    try:
        _poll(lambda: _host_pids(marker), what=f"{marker} alive inside the keeper")
        # The marker can only be host-visible via the joined command, so reaching
        # here means the keeper really opened; this also fails loudly if a future
        # edit puts the marker back on the driver's argv.
        assert str(child.pid) not in _host_pids(marker)
        child.kill()
        child.wait(timeout=30)
        _poll(lambda: not _host_pids(marker),
              what=f"{marker} gone after imp was SIGKILLed")
    finally:
        child.kill()
        for pid in _host_pids(marker):      # only reached if the switch failed
            with contextlib.suppress(OSError, ValueError):
                os.kill(int(pid), signal.SIGKILL)
        shutil.rmtree(root, ignore_errors=True)


def test_the_returncode_invariant_holds_on_a_joined_command():
    # AC 12a. nsenter re-raises a self-inflicted signal ON ITSELF where bwrap
    # mapped it to 128+n, so without the read-site normalization the agent's own
    # `kill -9 $$` would report -9 and read as imp having killed its tool.
    root = _make_root()
    try:
        ctx = _ctx(root)
        assert _bash(ctx, "kill -9 $$")["returncode"] == 137
        assert _bash(ctx, "kill -15 $$")["returncode"] == 143
        assert _bash(ctx, "exit 7")["returncode"] == 7
        assert _bash(ctx, "true")["returncode"] == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_command_imp_kills_reports_a_negative_returncode():
    # AC 12b, the other side of the same invariant: the SIGN is what a provider
    # turns into killed_tool_uses, so imp's own group kill must stay negative
    # where the command's own signal does not. This pins the half that moved —
    # a REAL imp kill through a join still yields a negative code. The other half,
    # negative code -> killed_tool_uses, is unchanged by this quest and already
    # pinned per provider against a stubbed rc: test_claude_code_wrap.py:285 and
    # test_codex_mcp_dispatch.py::test_negative_rc_bash_records_killed_tool.
    root = _make_root()
    try:
        ctx = _ctx(root)
        ready = root / "up"
        out: dict[str, dict] = {}

        async def _runner() -> None:
            out["r"] = json.loads(await get_registry()["bash"].async_invoke(
                ctx, {"command": f"touch {ready}; sleep 120"}))

        async def _killer() -> None:
            # Generous inner budget: it never false-fires under a saturated
            # -n auto run, and the outer fail_after catches a genuine hang.
            deadline = time.monotonic() + 30.0
            while not ready.exists():
                assert time.monotonic() < deadline, "the command never signalled ready"
                await anyio.sleep(0.02)
            await terminate_tool_processes(ctx)

        async def _scenario() -> None:
            with anyio.fail_after(90.0):      # single outer hang-catcher
                async with anyio.create_task_group() as tg:
                    tg.start_soon(_runner)
                    tg.start_soon(_killer)

        anyio.run(_scenario)
        assert out["r"]["returncode"] < 0, (
            f"imp's own kill did not read as a kill: {out['r']}")
    finally:
        shutil.rmtree(root, ignore_errors=True)


def _ns_id(ctx: ToolContext) -> str:
    """The pid namespace a call actually landed in — the identity probe that tells
    reuse from rebuild without depending on any object."""
    done = _bash(ctx, "readlink /proc/self/ns/pid")
    assert done["returncode"] == 0, done
    return done["stdout"].strip()


def test_a_sandbox_off_toggle_suspends_joining_without_a_rebuild():
    # AC 17 and AC 18d. ctx.sandbox_policy is DERIVED — it consults
    # coordinator.unsandboxed on every read and never touches base_sandbox_policy —
    # so the key is unchanged and the off-window commands simply run the
    # unsandboxed shape. Keying on the effective policy would rebuild on every
    # toggle and kill the agent's own background work mid-run.
    root = _make_root()
    marker = f"imp-run-{uuid.uuid4().hex}"
    try:
        ctx = _ctx(root)
        assert _bash(
            ctx, f"nohup setsid sh -c '{_ALIVE}' {marker} >/dev/null 2>&1 &"
        )["returncode"] == 0
        _poll(lambda: _host_pids(marker), what=f"{marker} alive host-side")
        inside = _ns_id(ctx)
        ns_before = ctx.sandbox_ns

        ctx.coordinator.unsandboxed = True
        off = _ns_id(ctx)
        assert off != inside, "the off-window command still joined the namespace"
        assert off == os.readlink("/proc/self/ns/pid")   # it ran on the host
        assert ctx.sandbox_ns is ns_before               # nothing was rebuilt

        ctx.coordinator.unsandboxed = False
        assert _ns_id(ctx) == inside                     # rejoined the same one
        assert _host_pids(marker), "the toggle killed the run's own background work"
        assert _bash(ctx, f"pgrep -f {marker}")["returncode"] == 0
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_a_base_policy_change_rebuilds_the_namespace():
    # AC 18. create_and_enter_worktree is the only site in src/ that reassigns
    # ctx.base_sandbox_policy and ctx.project_root mid-run
    # (create_and_enter_worktree.py:124-131, pinned by
    # tests/test_create_and_enter_worktree_tool.py); this is what that reassignment
    # must then cause. A keeper pinned to the pre-materialize plan would leave
    # every later call unable to see the worktree the agent was just told it is in.
    root = _make_root()
    fresh = _make_root()
    marker = f"imp-run-{uuid.uuid4().hex}"
    try:
        ctx = _ctx(root)
        ctx.background_enabled = True
        started = _bash(ctx, f"sh -c '{_ALIVE}' {marker}", run_in_background=True)
        _poll(lambda: _host_pids(marker), what=f"{marker} alive host-side")
        before = _ns_id(ctx)

        # exactly what the tool does when the worktree materializes: rewrite
        # project_root and repoint the base policy at the new tree. (The tool also
        # replaces git_worktree_self; either bind-plan field alone changes the
        # value, and it is the value that is the key.)
        ctx.project_root = fresh
        ctx.base_sandbox_policy = _policy_for(fresh)

        # (a) the join carries the POST-materialize bind plan
        wrote = _bash(ctx, 'pwd; echo NEW > new.txt; cat new.txt')
        assert wrote["returncode"] == 0, wrote
        assert wrote["stdout"].splitlines()[0] == str(fresh)
        assert "NEW" in wrote["stdout"]
        assert (fresh / "new.txt").read_text() == "NEW\n"
        assert _ns_id(ctx) != before                  # a different namespace

        # (b) the namespace opened before the change is closed
        _poll(lambda: not _host_pids(marker),
              what="the pre-rebuild process gone with its namespace")

        # (c) and it was killed through the normal path, with an explanation
        done = json.loads(anyio.run(
            get_registry()["bash_output"].async_invoke, ctx, {"id": started["id"]}))
        assert done["kill_reason"] == "sandbox-rebuilt"
        assert done["status"] == "killed"
    finally:
        shutil.rmtree(root, ignore_errors=True)
        shutil.rmtree(fresh, ignore_errors=True)


def test_a_steer_leaves_the_namespace_usable():
    # AC 19. The cooperative-steer branch kills and then RE-QUERIES with the same
    # ctx (claude_code.py:1859 -> _requery_pending -> return False). Closing there
    # would destroy the namespace while the run continues, and the pinned fds fail
    # closed — every later bash call would die "nsenter: fork failed".
    root = _make_root()
    marker = f"imp-run-{uuid.uuid4().hex}"
    try:
        ctx = _ctx(root)
        ctx.background_enabled = True
        _bash(ctx, 'echo BEFORE > "$HOME/steer.txt"')
        _bash(ctx, f"sh -c '{_ALIVE}' {marker}", run_in_background=True)
        _poll(lambda: _host_pids(marker), what=f"{marker} alive host-side")
        before = _ns_id(ctx)
        ns_before = ctx.sandbox_ns

        anyio.run(terminate_tool_processes, ctx)      # what the steer site does

        # (b) the kills still happen
        _poll(lambda: not _host_pids(marker),
              what="the process running across the steer terminated")
        # (a) and the next call succeeds rather than failing closed
        after = _bash(ctx, 'cat "$HOME/steer.txt"')
        assert after["returncode"] == 0, f"the steer broke the namespace: {after}"
        assert "BEFORE" in after["stdout"]
        # (c) reused, not rebuilt. The $HOME read above does NOT discriminate on
        # this policy — _policy_for puts HOME inside the worktree with no overlay
        # root, so that file would survive a rebuild too; these two carry it.
        assert ctx.sandbox_ns is ns_before
        assert _ns_id(ctx) == before
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_two_contexts_do_not_share_a_process_table():
    # AC 4. A sub_agent dispatch builds its OWN ToolContext, never shared with the
    # parent, and subagents run CONCURRENTLY — a step-wide namespace would put
    # parallel explorers in one process table where each could pkill the others'
    # work. The rule is one sentence: your processes live as long as you do.
    root_a, root_b = _make_root(), _make_root()
    mark_a = f"imp-a-{uuid.uuid4().hex}"
    mark_b = f"imp-b-{uuid.uuid4().hex}"
    try:
        ctx_a, ctx_b = _ctx(root_a), _ctx(root_b)
        for ctx, mark in ((ctx_a, mark_a), (ctx_b, mark_b)):
            assert _bash(
                ctx, f"nohup setsid sh -c '{_ALIVE}' {mark} >/dev/null 2>&1 &"
            )["returncode"] == 0
        _poll(lambda: _host_pids(mark_a) and _host_pids(mark_b),
              what="both subagents' processes alive host-side")

        # (a) each sees only its own
        assert _bash(ctx_a, f"pgrep -f {mark_a}")["returncode"] == 0
        assert _bash(ctx_a, f"pgrep -f {mark_b}")["returncode"] == 1
        assert _bash(ctx_b, f"pgrep -f {mark_a}")["returncode"] == 1

        # (b) ending one leaves the other untouched
        anyio.run(teardown_tool_context, ctx_a)
        _poll(lambda: not _host_pids(mark_a), what="the first subagent's process gone")
        assert _host_pids(mark_b), "ending one subagent killed the other's work"
        assert _bash(ctx_b, f"pgrep -f {mark_b}")["returncode"] == 0
    finally:
        shutil.rmtree(root_a, ignore_errors=True)
        shutil.rmtree(root_b, ignore_errors=True)


def test_a_call_can_use_a_cwd_that_exists_only_inside_the_keeper():
    # Spec §1: the join's cwd is set by the inner `cd`, never Popen's `cwd=`,
    # which resolves in the CALLER's mount namespace. Passing it also demanded
    # the directory exist HOST-side, and a miss raised FileNotFoundError out of
    # the spawn instead of reporting a shell error. Reachable, not theoretical:
    # a --tmpfs mask (here) and §5's per-run $HOME overlay both hold directories
    # with no host-side path at all.
    root = _make_root()
    masked = root / "masked"
    masked.mkdir()
    try:
        ctx = _ctx(root)
        ctx.base_sandbox_policy = _policy_for(root, secret_dirs=(str(masked),))
        ctx.background_enabled = True
        inside = masked / "only-inside"
        relative_inside = str(inside.relative_to(root))

        made = _bash(ctx, f"mkdir -p {inside}")
        assert made["returncode"] == 0, made
        assert not inside.exists()

        done = _bash(ctx, "pwd", cwd=relative_inside)
        assert done["returncode"] == 0, done
        assert done["stdout"].strip() == str(inside)

        beacon = root / "bg-cwd.txt"
        started = _bash(
            ctx,
            f"pwd > {beacon}",
            cwd=relative_inside,
            run_in_background=True,
        )
        assert started["status"] == "running", started
        assert _poll(
            lambda: beacon.read_text().strip() if beacon.exists() else "",
            what="the background call's own pwd",
        ) == str(inside)

        # (c) a cwd in NEITHER view is now a shell error the agent can read
        missing = root / "nope"
        gone = _bash(ctx, "pwd", cwd=str(missing))
        assert gone["returncode"] != 0
        assert str(missing) in gone["stderr"]
    finally:
        shutil.rmtree(root, ignore_errors=True)
