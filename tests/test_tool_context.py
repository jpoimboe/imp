# tests/test_tool_context.py
from __future__ import annotations

import asyncio
import contextlib
import errno
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest

from imp.agents.tools import context as ctx_mod
from imp.agents.tools.context import ToolContext, make_impty_sandbox_hook
from imp.agents.tools.errors import PermissionDenied
from imp.engine.sandbox import SandboxOpenFailed, SandboxPolicy, SandboxSpawn
from imp.paths import project_state_dir
from tests.conftest import _pid_gone_or_zombie
from tests.impty.e2e_support import can_pty


def _await_death(pid: int, complaint: str) -> None:
    """Poll until `pid` is gone or a zombie, then return; fail with `complaint` if it
    is still running at the budget. Observing the pid POSITIVELY is what keeps a
    starved survivor under a saturated -n auto run from reading as a pass: a process
    that has not been scheduled is still present in /proc."""
    deadline = time.monotonic() + 20.0     # generous; only burned on the failure path
    while not _pid_gone_or_zombie(pid):
        if time.monotonic() > deadline:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)   # don't leak the runaway on failure
            pytest.fail(complaint)
        time.sleep(0.05)


def test_resolve_project_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # Paths are now worktree-relative; /project/ prefix is gone.
    assert ctx.resolve_path("src/x.py") == (tmp_path / "src" / "x.py").resolve()


@pytest.mark.parametrize(
    ("cwd", "expected_suffix"),
    [
        (None, ""),
        ("", ""),
        (".", ""),
        ("nested/child", "nested/child"),
    ],
    ids=["omitted", "empty", "dot", "relative"],
)
def test_resolve_cwd_uses_the_live_project_root(
    tmp_path: Path, cwd: str | None, expected_suffix: str,
) -> None:
    ctx = _ctx(tmp_path)

    assert ctx.resolve_cwd(cwd) == str(tmp_path / expected_suffix)


def test_resolve_cwd_reads_the_current_project_root(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    worktree = tmp_path / "worktree"
    ctx.project_root = worktree

    assert ctx.resolve_cwd(".") == str(worktree)


def test_resolve_cwd_joins_relative_paths_lexically(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    assert ctx.resolve_cwd("alias/missing") == str(alias / "missing")


@pytest.mark.parametrize(
    "absolute_suffix",
    ["/masked/../only-inside", "//repeated///separators"],
    ids=["parent-segment", "repeated-separators"],
)
def test_resolve_cwd_preserves_absolute_spelling(
    tmp_path: Path, absolute_suffix: str,
) -> None:
    ctx = _ctx(tmp_path)
    absolute = f"{tmp_path}{absolute_suffix}"

    assert ctx.resolve_cwd(absolute) == absolute


def test_quest_context_field(tmp_path: Path) -> None:
    from imp.agents.runner import QuestContext
    ctx = _ctx(tmp_path)
    assert ctx.quest_context is None
    ctx.quest_context = QuestContext(
        title="Test change", project_root=tmp_path,
        workflow_dir=project_state_dir(tmp_path) / "quests" / "test-quest" / "workflow")
    assert ctx.quest_context.project_root == tmp_path


def _ctx(tmp_path: Path) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
        events_path=tmp_path / "events.jsonl",
    )


def test_running_processes_starts_empty(tmp_path):
    from imp.agents.tools.context import ToolContext

    ctx = ToolContext(
        project_root=tmp_path, events_path=tmp_path / "events.jsonl",
    )
    assert ctx.running_processes == []


def test_terminate_running_processes_drains(tmp_path):
    """terminate_running_processes signals SIGTERM, waits, then SIGKILL.
    The registry is cleared regardless of whether the process honored SIGTERM."""
    import anyio

    from imp.agents.tools.context import ToolContext, terminate_running_processes

    ctx = ToolContext(
        project_root=tmp_path, events_path=tmp_path / "events.jsonl",
    )

    async def _spawn_and_kill():
        # Use anyio.open_process so the fixture is realistic.
        proc = await anyio.open_process(
            ["sh", "-c", "sleep 5"], start_new_session=True,
        )
        ctx.running_processes.append(proc)
        await terminate_running_processes(ctx, timeout=0.5)
        # After termination the list is drained.
        assert ctx.running_processes == []
        # The process exit status is set (poll() returns non-None);
        # cleanup of fds happens via anyio's __aexit__ or our drain code.
        await proc.wait()
        assert proc.returncode is not None

    anyio.run(_spawn_and_kill)


def test_terminate_kills_process_group(tmp_path):
    """A shell that spawns a child: killpg must reap the WHOLE group, not just
    the shell pid. The child writes a marker file; after teardown it must stop.

    M6: this is the most timing-sensitive test in the plan (real sleeps + a real
    process group). Keep the sleeps as small as the assertion tolerates; if it
    flakes under CI load, widen the post-teardown observation window, not the cap.
    """
    import anyio

    from imp.agents.tools.context import ToolContext, terminate_running_processes

    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    marker = tmp_path / "child_alive"

    async def go():
        # Parent shell backgrounds a child that touches `marker` every 0.1s.
        proc = await anyio.open_process(
            ["sh", "-c", f"(while true; do touch {marker}; sleep 0.1; done) & wait"],
            start_new_session=True,
        )
        ctx.running_processes.append(proc)
        await anyio.sleep(0.3)
        await terminate_running_processes(ctx)
        assert ctx.running_processes == []
        # Give any orphaned child a chance to keep writing, then confirm it died.
        if marker.exists():
            marker.unlink()
        await anyio.sleep(0.5)
        assert not marker.exists(), "child survived killpg — group was not reaped"

    anyio.run(go)


def test_terminate_uses_named_grace_constant():
    # The grace is a named constant, not a 2.0 literal (must be 4.0).
    import inspect

    from imp.agents.timeouts import PROCESS_KILL_GRACE_S
    from imp.agents.tools import context
    sig = inspect.signature(context.terminate_running_processes)
    assert sig.parameters["timeout"].default == PROCESS_KILL_GRACE_S
    assert PROCESS_KILL_GRACE_S == 4.0


def test_terminate_lets_a_group_member_finish_its_sigterm_trap(tmp_path: Path) -> None:
    """The kill grace belongs to the GROUP, not to the registered leader. The leader
    (`sh`) dies on SIGTERM at ~0s — exactly what the outer bwrap of a sandboxed
    command does — while a group member is still running its own SIGTERM handler.
    Ending the grace at the leader's exit SIGKILLs that member mid-cleanup, so its
    marker write never lands. Spawned to DEVNULL, not to inherited pipes: the
    foreground bash tool spawns to files, and a pipe the member holds open would keep
    proc.wait() blocked until the member exits and hide the bug entirely."""
    import anyio

    from imp.agents.tools.context import ToolContext, terminate_running_processes

    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    ready = tmp_path / "member-up"
    marker = tmp_path / "trap-finished"
    prog = ("import pathlib, signal, sys, time\n"
            "def _trap(sig, frame):\n"
            "    time.sleep(0.3)\n"
            "    pathlib.Path(sys.argv[2]).write_text('done')\n"
            "    sys.exit(0)\n"
            "signal.signal(signal.SIGTERM, _trap)\n"
            "pathlib.Path(sys.argv[1]).write_text('up')\n"
            "time.sleep(3600)\n")

    async def go() -> None:
        proc = await anyio.open_process(
            ["sh", "-c",
             f"{shlex.quote(sys.executable)} -c {shlex.quote(prog)} "
             f"{shlex.quote(str(ready))} {shlex.quote(str(marker))} & wait"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        ctx.running_processes.append(proc)
        deadline = time.monotonic() + 20.0     # generous: only bounds process startup
        while not ready.exists():
            assert time.monotonic() < deadline, "the group member never started"
            await anyio.sleep(0.02)
        # 8.0, not the 4.0 default: the member's 0.3s handler then has ~25x headroom,
        # so a saturated -n auto run cannot race it. Free on the passing path —
        # teardown returns as soon as the group empties.
        await terminate_running_processes(ctx, timeout=8.0)

    anyio.run(go)

    assert marker.exists(), (
        "the member's SIGTERM handler was SIGKILLed inside the grace — the grace was "
        "keyed on the leader's exit, not on the group")


def test_terminate_sigkills_a_member_spawned_during_the_grace(tmp_path: Path) -> None:
    """The other end of the same teardown: escalation must not be skipped just because
    the leader was reaped. The leader's SIGTERM trap spawns a NEW in-group member and
    exits, so that member is in neither by-pid capture — not the one taken before the
    SIGTERM, nor the post-grace re-enumeration, whose PPid links died with the parent.
    Only an unconditional group SIGKILL reaches it."""
    import anyio

    from imp.agents.tools.context import ToolContext, terminate_running_processes

    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    ready = tmp_path / "leader-up"
    late_pid = tmp_path / "late.pid"
    member = tmp_path / "member.py"
    member.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(3600)\n")
    script = tmp_path / "run.sh"
    script.write_text(
        f"trap '{shlex.quote(sys.executable)} {shlex.quote(str(member))} "
        f"{shlex.quote(str(late_pid))} & exit 0' TERM\n"
        f"touch {shlex.quote(str(ready))}\n"
        "while :; do sleep 0.2; done\n")

    async def go() -> None:
        proc = await anyio.open_process(
            ["sh", str(script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        ctx.running_processes.append(proc)
        deadline = time.monotonic() + 20.0     # generous: only bounds process startup
        while not ready.exists():
            assert time.monotonic() < deadline, "the leader never started"
            await anyio.sleep(0.02)
        await terminate_running_processes(ctx, timeout=2.0)

    anyio.run(go)

    deadline = time.monotonic() + 20.0
    while not (late_pid.exists() and late_pid.read_text().strip()):
        if time.monotonic() > deadline:
            pytest.fail("the trap never spawned the late member")
        time.sleep(0.02)
    _await_death(
        int(late_pid.read_text()),
        "a member spawned during the grace outlived teardown — the group SIGKILL was "
        "skipped because the leader had already been reaped")


def test_terminate_reaps_the_group_even_when_the_caller_deadline_fires(
    tmp_path: Path,
) -> None:
    """Holding the grace open for the group means a CALLER's deadline can now land
    inside it: signals.py:629 gives each att.interrupt() 2.0s, and agent_run.py:179
    caps interrupt()+aclose() at the grace's own 4.0s. The registry is drained at
    entry, so a cancellation that unwinds past the escalation leaks the whole group
    with nothing left to retry it — and skips the provider's following
    terminate_background_processes. The escalation must therefore be shielded. The
    leader exits on SIGTERM at ~0s (the sandboxed bwrap shape) while a TERM-ignoring
    member keeps the group alive, so the poll is still running when the deadline
    fires."""
    import anyio

    from imp.agents.tools.context import ToolContext, terminate_running_processes

    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    ready = tmp_path / "member.pid"
    member = tmp_path / "member.py"
    member.write_text(
        "import os, pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()))\n"
        "time.sleep(3600)\n")
    script = tmp_path / "run.sh"
    script.write_text(
        f"{shlex.quote(sys.executable)} {shlex.quote(str(member))} "
        f"{shlex.quote(str(ready))} &\n"
        "wait\n")

    async def go() -> None:
        proc = await anyio.open_process(
            ["sh", str(script)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        ctx.running_processes.append(proc)
        deadline = time.monotonic() + 20.0     # generous: only bounds process startup
        while not (ready.exists() and ready.read_text().strip()):
            assert time.monotonic() < deadline, "the group member never started"
            await anyio.sleep(0.02)
        # 0.5 well under the 8.0 grace, so the deadline lands mid-poll — the shape
        # signals.py's 2.0s budget produces against the real 4.0s grace.
        with anyio.move_on_after(0.5) as scope:
            await terminate_running_processes(ctx, timeout=8.0)
        assert scope.cancelled_caught, (
            "the caller deadline did not fire — the test is not exercising the hazard")

    anyio.run(go)
    _await_death(
        int(ready.read_text()),
        "the group survived a caller deadline that fired mid-grace — the escalation "
        "was cancelled and the drained registry leaves nothing to retry it")


def test_terminate_reaps_all_groups_when_caller_deadline_fires_mid_escalation(
    tmp_path: Path,
) -> None:
    """The leader here ignores SIGTERM, so it is still unreaped when the escalation
    runs; the caller's cancellation is therefore delivered at proc #1's ``await
    p.wait()`` inside the escalation loop, and only ``CancelScope(shield=True)``
    carries the loop on to proc #2.  The existing single-proc guard cannot pin this:
    with one leader that exits on SIGTERM the wait returns synchronously and no
    cancellation can land inside the loop."""
    import anyio

    from imp.agents.tools.context import ToolContext, terminate_running_processes

    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    ready1 = tmp_path / "ready1"
    ready2 = tmp_path / "ready2"
    script = tmp_path / "leader.py"
    script.write_text(
        "import pathlib, signal, sys, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "pathlib.Path(sys.argv[1]).write_text('ready')\n"
        "time.sleep(3600)\n")
    pids: list[int] = []

    async def go() -> None:
        proc1 = await anyio.open_process(
            [sys.executable, str(script), str(ready1)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        ctx.running_processes.append(proc1)
        pids.append(proc1.pid)
        proc2 = await anyio.open_process(
            [sys.executable, str(script), str(ready2)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        ctx.running_processes.append(proc2)
        pids.append(proc2.pid)
        deadline = time.monotonic() + 20.0     # generous: only bounds process startup
        while not (ready1.exists() and ready2.exists()):
            assert time.monotonic() < deadline, "the leaders never installed their SIGTERM handlers"
            await anyio.sleep(0.02)
        # 0.5 well under the 8.0 grace — the deadline lands mid-grace, and during the
        # shielded escalation at proc #1's await p.wait() inside the loop.
        with anyio.move_on_after(0.5) as scope:
            await terminate_running_processes(ctx, timeout=8.0)
        assert scope.cancelled_caught, (
            "the caller deadline did not fire — the test is not exercising the hazard")

    anyio.run(go)
    _await_death(
        pids[0],
        "the first group survived a caller deadline that fired mid-grace")
    _await_death(
        pids[1],
        "the escalation was abandoned partway through the registry, leaving a later "
        "process group alive with only a SIGTERM it ignores")


def test_tool_context_has_event_log_field(tmp_path) -> None:
    from imp.agents.tools.context import ToolContext
    ctx = ToolContext(
        project_root=tmp_path, events_path=tmp_path / "events.jsonl",
    )
    assert ctx.event_log is None
    sentinel = object()
    ctx.event_log = sentinel
    assert ctx.event_log is sentinel


def test_tool_context_resolved_tiers_defaults_none(tmp_path):
    from imp.agents.tools.context import ToolContext
    ctx = ToolContext(
        project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    assert ctx.resolved_tiers is None


def test_tool_context_tiers_defaults_empty(tmp_path) -> None:
    from imp.agents.tools.context import ToolContext
    ctx = ToolContext(
        project_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert ctx.tiers == ()
    ctx.tiers = ("low", "high")
    assert ctx.tiers == ("low", "high")


# --- resolve_path / authorize_fs tests ---

def test_resolve_path_relative_maps_under_root(tmp_path):
    ctx = _ctx(tmp_path)
    assert ctx.resolve_path("src/foo.py") == (tmp_path / "src" / "foo.py").resolve()


def test_resolve_path_returns_out_of_worktree_without_raise(tmp_path: Path) -> None:
    # New contract: resolve_path no longer fences the worktree — it resolves
    # and returns. authorize_fs is the gate now.
    ctx = _ctx(tmp_path)
    assert ctx.resolve_path("/etc/passwd") == Path("/etc/passwd")
    assert ctx.resolve_path("/artifacts/spec.md") == Path("/artifacts/spec.md")


def test_resolve_path_relative_still_maps_under_root(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert ctx.resolve_path("src/x.py") == (tmp_path / "src" / "x.py").resolve()


def test_resolve_path_expands_home(tmp_path: Path, monkeypatch) -> None:
    # A `~`-home path resolves to the literal host location, NOT under the
    # worktree root — so an agent in a Quest worktree can reach a file the user
    # named by ~/path in their main checkout.
    home = tmp_path / "home" / "u"
    monkeypatch.setenv("HOME", str(home))
    ctx = _ctx(tmp_path)
    assert ctx.resolve_path("~/imp/docs/bug.md") == (home / "imp" / "docs" / "bug.md").resolve()
    # A bare ~ component is expanded, not treated as a literal dir under the root.
    assert ctx.resolve_path("~") == home.resolve()


def _active_policy(tmp_path: Path, *, readonly_overlays: tuple[str, ...] = (),
                   quest_tmp_dir: str = "/tmp/imp-1000/abc") -> SandboxPolicy:
    home = tmp_path.parent / "changes_abc"
    return SandboxPolicy(
        active=True, worktree=str(tmp_path), git_common_dir=str(tmp_path / ".git"),
        quest_home=str(home),
        git_ro_remask=(), writable_caches=(), extra_readable=(), extra_writable=(),
        secret_dirs=("/home/u/.ssh",), secret_files=("/home/u/.netrc",),
        child_env=(), network=True, note="", readonly_overlays=readonly_overlays,
        quest_tmp_dir=quest_tmp_dir,
    )


def test_authorize_fs_worktree_writable_by_default_when_unsandboxed(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)                       # sandbox_policy None → bash parity
    assert ctx.authorize_fs(("write",), "a.txt") == (tmp_path / "a.txt").resolve()
    assert ctx.authorize_fs(("read",), "a.txt") == (tmp_path / "a.txt").resolve()


def test_authorize_fs_worktree_writable_under_active_policy(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    assert ctx.authorize_fs(("write",), "src/x.py") == (tmp_path / "src" / "x.py").resolve()


def test_authorize_fs_quest_home_writable_by_default(tmp_path: Path) -> None:
    # The finalize regression: a progress.md write under quest_home with no
    # special config must succeed (quest_home is an rw bind).
    pol = _active_policy(tmp_path)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = pol
    target = Path(pol.quest_home) / "progress.md"
    assert ctx.authorize_fs(("write",), str(target)) == target.resolve()


def test_authorize_fs_read_only_overlay_denies_worktree_allows_quest_home(tmp_path: Path) -> None:
    pol = _active_policy(tmp_path, readonly_overlays=(str(tmp_path),))
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = pol
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("write",), "src/x.py")            # worktree locked
    target = Path(pol.quest_home) / "progress.md"
    assert ctx.authorize_fs(("write",), str(target)) == target.resolve()  # quest_home stays rw


def test_authorize_fs_out_of_worktree_unsandboxed_allows(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)                        # sandbox_policy is None → bash parity
    out = tmp_path.parent / "outside.txt"
    assert ctx.authorize_fs(("write",), str(out)) == out.resolve()
    assert ctx.authorize_fs(("read",), str(out)) == out.resolve()


def test_authorize_fs_out_of_worktree_sandboxed_read(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    # secret-masked read denied; ordinary out-of-worktree read allowed
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("read",), "/home/u/.ssh/id_rsa")
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("read",), "/home/u/.netrc")
    assert ctx.authorize_fs(("read",), "/usr/share/x") == Path("/usr/share/x")


def test_authorize_fs_out_of_worktree_sandboxed_write(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    pol = _active_policy(tmp_path)
    ctx.base_sandbox_policy = pol
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("write",), "/tmp/scratch.txt")     # host /tmp: read-only floor
    # ...but the per-quest scratch dir under it is writable, and /tmp stays readable.
    scratch = Path(pol.quest_tmp_dir) / "scratch.txt"
    assert ctx.authorize_fs(("write",), str(scratch)) == scratch
    assert ctx.authorize_fs(("read",), "/tmp/scratch.txt") == Path("/tmp/scratch.txt")
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("write",), "/usr/lib/x")          # no writable bind
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("write",), "/home/u/.ssh/id_rsa")  # secret mask


def test_authorize_fs_denial_message_hint_is_scoped_to_slash_tmp_writes(
    tmp_path: Path,
) -> None:
    # AC 7(b): only a write under /tmp earns the scratch-dir hint. A write denied
    # elsewhere, and any read denial, keeps the message it has today.
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)

    with pytest.raises(PermissionDenied) as exc:
        ctx.authorize_fs(("write",), "/usr/lib/x")
    assert str(exc.value) == "write on /usr/lib/x denied by the sandbox policy"

    with pytest.raises(PermissionDenied) as exc:
        ctx.authorize_fs(("read",), "/home/u/.ssh/id_rsa")
    assert str(exc.value) == (
        "read on /home/u/.ssh/id_rsa denied by the sandbox policy")


def test_authorize_fs_slash_tmp_hint_is_omitted_without_a_scratch_dir(
    tmp_path: Path,
) -> None:
    # AC 7(c): a policy with no scratch dir must never render "under $TMPDIR ()".
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path, quest_tmp_dir="")

    with pytest.raises(PermissionDenied) as exc:
        ctx.authorize_fs(("write",), "/tmp/scratch.txt")

    assert str(exc.value) == (
        "write on /tmp/scratch.txt denied by the sandbox policy")
    assert "$TMPDIR" not in str(exc.value)


def test_background_registry_and_bg_ids(tmp_path) -> None:
    from imp.agents.tools.context import ToolContext
    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert ctx.background_processes == {}
    assert ctx.next_bg_id() == "bg1"
    assert ctx.next_bg_id() == "bg2"
    # distinct contexts do not share the counter
    ctx2 = ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert ctx2.next_bg_id() == "bg1"


def test_get_parallel_limiter_is_lazy_and_per_invocation(tmp_path: Path) -> None:
    """The limiter is created once on first call (identity stable across calls),
    its width equals the resolved knob, and a SEPARATE ToolContext gets its OWN
    limiter (non-inheritance / per-invocation isolation — the nested-pool
    deadlock guard)."""
    from imp.agents.parallel import max_parallel_tasks, reset_parallelism_cache
    from imp.agents.tools.context import ToolContext

    reset_parallelism_cache()
    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert ctx.parallel_limiter is None        # not created until asked
    lim = ctx.get_parallel_limiter()
    assert lim is ctx.get_parallel_limiter()   # check-then-set, single object
    assert lim.total_tokens == max_parallel_tasks()
    other = ToolContext(project_root=tmp_path, events_path=tmp_path / "events.jsonl")
    assert other.get_parallel_limiter() is not lim  # fresh limiter per invocation
    reset_parallelism_cache()


def test_terminate_reaps_regrouped_descendant(tmp_path):
    """A descendant that calls setsid() LEAVES the tool's process group, so killpg
    alone misses it — but it keeps the parent as ppid, so the /proc walk finds it.
    After terminate_running_processes the descendant must be gone (not reparented
    to init and surviving)."""
    import os
    import sys
    from pathlib import Path

    import anyio
    import pytest

    from imp.agents.tools.context import ToolContext, terminate_running_processes

    if not Path("/proc").is_dir():
        pytest.skip("descendant reaping needs /proc (Linux)")

    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    pidfile = tmp_path / "descendant.pid"
    # Parent (registered, its own session via start_new_session) forks a child that
    # setsid()s — leaving the parent's process group — writes its pid, then sleeps.
    script = (
        "import os, sys, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.setsid()\n"
        "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        "    time.sleep(300)\n"
        "else:\n"
        "    time.sleep(300)\n"
    )

    async def go():
        proc = await anyio.open_process(
            [sys.executable, "-c", script, str(pidfile)],
            start_new_session=True,
        )
        ctx.running_processes.append(proc)
        for _ in range(250):
            if pidfile.exists() and pidfile.read_text().strip():
                break
            await anyio.sleep(0.02)
        descendant = int(pidfile.read_text().strip())
        os.kill(descendant, 0)  # alive (raises ProcessLookupError if not)

        await terminate_running_processes(ctx)
        assert ctx.running_processes == []

        gone = False
        for _ in range(250):
            try:
                os.kill(descendant, 0)
            except ProcessLookupError:
                gone = True
                break
            await anyio.sleep(0.02)
        assert gone, "re-grouped descendant survived the reaping path"

    anyio.run(go)


def test_tool_context_quest_home_defaults_none(tmp_path: Path) -> None:
    from imp.agents.tools.context import ToolContext
    ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
    assert ctx.quest_home is None
    ctx.quest_home = project_state_dir(tmp_path) / "quests" / "abc"
    assert ctx.quest_home == project_state_dir(tmp_path) / "quests" / "abc"
    assert ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl",
                       quest_home=tmp_path).quest_home == tmp_path


def test_sandbox_policy_is_live_property(tmp_path: Path) -> None:
    """sandbox_policy is a live computed property reading coordinator.unsandboxed."""
    from types import SimpleNamespace

    ctx = _ctx(tmp_path)
    pol = _active_policy(tmp_path)

    # No coordinator — base policy returned as-is.
    ctx.base_sandbox_policy = pol
    assert ctx.sandbox_policy is pol

    # Coordinator present but NOT unsandboxed — still base policy.
    ctx.coordinator = SimpleNamespace(unsandboxed=False)
    assert ctx.sandbox_policy is pol
    assert ctx.sandbox_policy.active is True

    # Coordinator flipped to unsandboxed=True — computed inactive copy returned.
    ctx.coordinator = SimpleNamespace(unsandboxed=True)
    result = ctx.sandbox_policy
    assert result is not pol
    assert result.active is False
    assert result.note == "unsandboxed_override"

    # Flip back — policy goes live again WITHOUT re-entering the step.
    ctx.coordinator = SimpleNamespace(unsandboxed=False)
    assert ctx.sandbox_policy.active is True

    # base_sandbox_policy None → None regardless of coordinator.
    ctx.base_sandbox_policy = None
    ctx.coordinator = SimpleNamespace(unsandboxed=True)
    assert ctx.sandbox_policy is None


def test_terminate_tool_processes_kills_both_registries(tmp_path: Path) -> None:
    # The mid-run-safe half of the split: foreground group, then background groups
    # with their own escalation — and NO namespace touched, because the
    # cooperative-steer branch re-queries with this same ctx.
    from imp.agents.tools.bg import spawn_background
    from imp.agents.tools.context import terminate_tool_processes

    async def main() -> None:
        ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
        ctx.agent_id = "step:build/coder"
        ctx.background_enabled = True
        sentinel = object()
        ctx.sandbox_ns = sentinel
        await spawn_background(
            ctx, command="while :; do sleep 5; done",
            argv=["sh", "-c", "while :; do sleep 5; done"], cwd=str(tmp_path),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        bp = next(iter(ctx.background_processes.values()))
        await terminate_tool_processes(ctx)
        assert bp.status == "killed"
        assert bp.kill_reason == "interrupted"
        assert ctx.sandbox_ns is sentinel      # the namespace is NOT touched

    anyio.run(main)


def test_terminate_tool_processes_can_name_the_rebuild_reason(tmp_path: Path) -> None:
    # A rebuild kills through the normal path, so a background entry gets its
    # tombstone and an explanation instead of a bare negative returncode.
    from imp.agents.tools.bg import spawn_background
    from imp.agents.tools.context import terminate_tool_processes

    async def main() -> None:
        ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
        ctx.agent_id = "step:build/coder"
        ctx.background_enabled = True
        await spawn_background(
            ctx, command="while :; do sleep 5; done",
            argv=["sh", "-c", "while :; do sleep 5; done"], cwd=str(tmp_path),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        bp = next(iter(ctx.background_processes.values()))
        await terminate_tool_processes(ctx, kill_reason="sandbox-rebuilt")
        assert bp.kill_reason == "sandbox-rebuilt"

    anyio.run(main)


def test_teardown_tool_context_closes_the_namespace_last(tmp_path: Path) -> None:
    # Order is the point: the background escalation COMPLETES before the close.
    # The namespace samples the registry AT CLOSE TIME, which is what
    # discriminates — asserting after teardown could not fail, since by then both
    # ran. The observable is the reaped returncode, recorded only after the
    # unconditional SIGKILL pass (bg.py:568-571); bp.status would be weaker, as it
    # flips in the pre-SIGTERM pass and so proves only that the escalation began.
    from imp.agents.tools.bg import spawn_background
    from imp.agents.tools.context import teardown_tool_context

    observed: dict[str, bool] = {}

    class _NS:
        # No base_policy: only ensure_tool_namespace reads it, and this path
        # never calls it (same as Task 5's sibling double).
        def __init__(self, ctx: ToolContext) -> None:
            self._ctx = ctx

        def close(self) -> None:
            bp = next(iter(self._ctx.background_processes.values()))
            observed["reaped_at_close"] = bp.returncode is not None

    async def main() -> None:
        ctx = ToolContext(project_root=tmp_path, events_path=tmp_path / "e.jsonl")
        ctx.agent_id = "step:build/coder"
        ctx.background_enabled = True
        ctx.sandbox_ns = _NS(ctx)
        await spawn_background(
            ctx, command="while :; do sleep 5; done",
            argv=["sh", "-c", "while :; do sleep 5; done"], cwd=str(tmp_path),
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")})
        await teardown_tool_context(ctx)
        assert observed["reaped_at_close"]
        assert ctx.sandbox_ns is None          # a second teardown is a no-op

    anyio.run(main)


def test_no_provider_still_calls_the_unsplit_pair() -> None:
    # Spec §3: close() appears at five sites that are all the same shape, and at
    # no branch that continues — the mid-run close is unrepresentable rather than
    # merely avoided. That holds only while no provider reaches past the split.
    from pathlib import Path as _Path

    from imp.providers import anthropic, claude_code, codex_runner, openai, scripted
    for mod in (anthropic, openai, claude_code, codex_runner, scripted):
        src = _Path(mod.__file__).read_text()
        assert "terminate_running_processes" not in src, mod.__name__
        assert "terminate_background_processes" not in src, mod.__name__


def test_the_conftest_owner_closes_a_registered_contexts_keeper(tmp_path: Path) -> None:
    # A directly-constructed ToolContext reaches no provider run-generator
    # finally, so the suite itself has to own the keeper. Driven through the
    # fixture's own body so the assertion does not have to outlive the test.
    from types import SimpleNamespace

    from imp.agents.tools.bg import BgProc
    from tests.conftest import close_owned_tool_contexts, own_tool_context

    closed: list[str] = []

    class _NS:
        def close(self) -> None:
            closed.append("closed")

    # The run dir does NOT exist: an adopted test rmtrees its root in its own
    # finally, before this fixture runs. A background teardown here would
    # mkdir(parents=True) it back to write tombstones — the stray-tree
    # regression — so the owner must close the namespace and nothing else.
    gone = tmp_path / "gone"
    ctx = ToolContext(project_root=tmp_path, events_path=gone / "e.jsonl")
    ctx.agent_id = "step:build/coder"
    ctx.sandbox_ns = _NS()
    ctx.background_processes["bg1"] = BgProc(
        id="bg1", proc=SimpleNamespace(pid=None, returncode=0),
        out_path=gone / "bg1.out", err_path=gone / "bg1.err", command="x")

    assert own_tool_context(ctx) is ctx      # returns it, so `ctx = own(...)` reads well
    close_owned_tool_contexts()

    assert closed == ["closed"]
    assert ctx.sandbox_ns is None
    assert not gone.exists()                # no tombstone write, no resurrected tree


# ---------------------------------------------------------------------------
# The ToolContext -> SandboxHook adapter an ImptyDriver spawns through
# ---------------------------------------------------------------------------


class _FakeNamespace:
    """Stands in for the run's keeper: join_exec's real signature and a
    recognisable argv/env/pass_fds triple, plus open_pty's fd pair. No bwrap host,
    and no keeper to leak — what is under test here is the adapter's composition,
    not the join's shape (tests/test_sandbox_namespace.py owns that)."""

    def __init__(self, pty_error: OSError | None = None) -> None:
        self.calls: list[tuple[list[str], str]] = []
        self.pty_calls = 0
        self._pty_error = pty_error

    def join_exec(self, argv, *, cwd, engine_env):
        self.calls.append((list(argv), cwd))
        return SandboxSpawn(
            argv=["nsenter", f"--wdns={cwd}", "--", *argv],
            env={"PATH": "/sandbox/bin", "HOME": "/sandbox/home", **dict(engine_env)},
            pass_fds=(7, 9))

    def open_pty(self) -> tuple[int, int]:
        self.pty_calls += 1
        if self._pty_error is not None:
            raise self._pty_error
        return (31, 32)     # recognisable, never handed to a real session


def _stub_ensure_namespace(monkeypatch: pytest.MonkeyPatch, ns: object,
                           *, fail: str | None = None) -> list[dict[str, object]]:
    """Replace the real (keeper-opening) acquisition with a recorder, and return
    the call list so a test can assert WHAT the adapter asked for and ON WHICH
    THREAD it was answered."""
    seen: list[dict[str, object]] = []

    async def _fake(ctx, *, engine_env, on_retry):
        seen.append({"engine_env": dict(engine_env),
                     "thread": threading.current_thread()})
        if fail is not None:
            raise SandboxOpenFailed(fail)
        return ns

    monkeypatch.setattr(ctx_mod, "ensure_tool_namespace", _fake)
    return seen


def _hook_from_worker(ctx: ToolContext, argv: list[str], cwd: str | None = None,
                      env: dict[str, str] | None = None):
    """Production's shape: the hook is built ON the loop (both providers build it
    in _enter) and called from a worker thread (both offload every driver call
    through asyncio.to_thread)."""
    async def _main():
        hook = make_impty_sandbox_hook(ctx)
        return await asyncio.to_thread(lambda: hook(argv, cwd, env))

    return asyncio.run(_main())


def test_the_impty_hook_declines_while_the_session_is_unsandboxed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # /sandbox off is expressed ONLY by the hook declining — no flag, no driver
    # state — and the EFFECTIVE policy is read per call, so a mid-run toggle is
    # honored by the next start() with nothing to remember.
    ns = _FakeNamespace()
    seen = _stub_ensure_namespace(monkeypatch, ns)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    ctx.coordinator = SimpleNamespace(unsandboxed=True, should_pause=False)
    assert _hook_from_worker(ctx, ["true"]) is None
    # No policy at all (sandbox disabled by config) declines by the same branch.
    ctx.coordinator = None
    ctx.base_sandbox_policy = None
    assert _hook_from_worker(ctx, ["true"]) is None
    # Declining must cost NOTHING: a bare spawn opens no keeper. Asserted, because
    # reading the policy after acquiring the namespace would look identical from
    # the driver's side and pay a keeper open per /sandbox off call.
    assert seen == []
    assert ns.calls == []


def test_the_impty_hook_plans_a_join_exec_with_the_cwd_and_an_env_overlay(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ns = _FakeNamespace()
    _stub_ensure_namespace(monkeypatch, ns)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    plan = _hook_from_worker(ctx, ["vim", "notes.txt"], None, {"API_KEY": "k"})
    assert plan is not None
    # The JOIN's argv, with the caller's exec'd at the tail — not the caller's
    # argv with a cwd hung off it.
    assert plan.argv[:2] == ["nsenter", f"--wdns={tmp_path}"]
    assert plan.argv[-2:] == ["vim", "notes.txt"]
    assert plan.pass_fds == (7, 9)
    # An overlay ON the curated child env, never a replacement: one variable must
    # not drop PATH, HOME and the IMP_* the sandbox contract depends on.
    assert plan.env["API_KEY"] == "k"
    assert plan.env["PATH"] == "/sandbox/bin"
    assert plan.env["HOME"] == "/sandbox/home"
    # cwd=None resolves to the project root, and it is join_exec that resolves it —
    # in the namespace's mount view, where Popen's cwd= and --wd= do not.
    assert ns.calls == [(["vim", "notes.txt"], str(tmp_path))]
    _hook_from_worker(ctx, ["true"], str(tmp_path / "sub"))
    assert ns.calls[-1] == (["true"], str(tmp_path / "sub"))


def test_the_impty_hook_asks_for_the_same_engine_env_bash_does(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Both paths must present the namespace IDENTICAL inputs, or a pty program
    # would land in a keeper built from a different picture than the run's shell
    # commands. The LIVE quest wins over the frozen snapshot, exactly as in bash,
    # so a mid-run rename_quest reaches the next pty spawn too.
    from imp.agents.runner import QuestContext

    ns = _FakeNamespace()
    seen = _stub_ensure_namespace(monkeypatch, ns)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    ctx.quest_context = QuestContext(id="frozen", title="Before the rename",
                                     project_root=tmp_path,
                                     workflow_dir=tmp_path / ".imp" / "wf")
    ctx.quest = SimpleNamespace(id="live01", title="Renamed quest",
                                quest_dir=tmp_path / ".imp" / "quests" / "live01")
    plan = _hook_from_worker(ctx, ["true"])
    eng = seen[0]["engine_env"]
    assert eng["IMP_QUEST_ID"] == "live01"
    assert eng["IMP_QUEST_TITLE"] == "Renamed quest"
    assert eng["IMP_QUEST_DIR"] == str(tmp_path / ".imp" / "quests" / "live01")
    assert eng["IMP_PROJECT_ROOT"] == str(tmp_path)
    # The same live values are handed to join_exec, so the CHILD env carries them
    # too — asking for one env and spawning with another would be silent.
    assert plan is not None
    assert plan.env["IMP_QUEST_TITLE"] == "Renamed quest"


def test_the_impty_hook_opens_the_namespace_on_the_runs_own_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # open() passes --die-with-parent, a PR_SET_PDEATHSIG the kernel fires when
    # the CREATING THREAD exits: a keeper opened on a pool worker dies the moment
    # that worker retires, taking the run's shell commands with it. The driver's
    # calls ARE on pool workers, so the acquisition has to cross back to the loop.
    ns = _FakeNamespace()
    seen = _stub_ensure_namespace(monkeypatch, ns)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    threads: dict[str, object] = {}

    async def _main():
        threads["loop"] = threading.current_thread()
        hook = make_impty_sandbox_hook(ctx)

        def _call():
            threads["worker"] = threading.current_thread()
            return hook(["true"], None, None)

        return await asyncio.to_thread(_call)

    assert asyncio.run(_main()) is not None
    assert threads["worker"] is not threads["loop"]   # really called off-loop
    assert seen[0]["thread"] is threads["loop"]       # ...and answered back on it


def test_the_impty_hook_refuses_to_block_the_loop_it_would_wait_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Called ON the loop, blocking for that loop's own result is a permanent
    # deadlock: the coroutine cannot start until the caller returns, and the
    # caller is waiting for the coroutine. Fail loudly rather than hang a whole
    # run to its outer timeout with no clue why.
    _stub_ensure_namespace(monkeypatch, _FakeNamespace())
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)

    async def _main() -> None:
        hook = make_impty_sandbox_hook(ctx)
        with pytest.raises(AssertionError, match="worker thread"):
            hook(["true"], None, None)

    asyncio.run(_main())


def test_a_failed_namespace_open_surfaces_as_the_terminal_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # AC 12: impty never learns the sandbox's exception types — the adapter
    # translates, and ImptyDriver.start's existing error surface carries the text
    # to the agent. Nothing is spawned: there is no pty and no exit status to
    # report, only why the program did not run.
    import impty.driver as impty_driver_mod
    from impty.driver import ImptyDriver

    _stub_ensure_namespace(monkeypatch, _FakeNamespace(),
                           fail="bwrap: execvp sleep: No such file or directory")
    spawns: list[object] = []
    monkeypatch.setattr(impty_driver_mod, "TerminalSession",
                        lambda *a, **k: spawns.append((a, k)))
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)

    async def _main() -> str:
        driver = ImptyDriver(sandbox=make_impty_sandbox_hook(ctx))
        try:
            await asyncio.to_thread(lambda: driver.start(["true"]))
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        return "no error raised"

    assert asyncio.run(_main()) == (
        "RuntimeError: sandbox could not start the terminal session: "
        "bwrap: execvp sleep: No such file or directory")
    assert spawns == []


def test_a_timed_out_namespace_acquire_surfaces_as_the_terminal_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A stalled loop (loop stopped before the future resolves) must not park the
    # worker thread forever — the bounded wait must surface the same diagnostic.
    from imp.agents.tools.context import _namespace_from_worker

    monkeypatch.setattr(ctx_mod, "NAMESPACE_ACQUIRE_TIMEOUT_S", 0.1)

    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)

    # A loop that is created but never run: coroutines scheduled on it never
    # execute, so .result(timeout=...) will always time out.
    idle_loop = asyncio.new_event_loop()
    try:
        eng: dict[str, str] = {}
        error: list[Exception] = []

        def _worker():
            try:
                _namespace_from_worker(ctx, idle_loop, eng, "true")
            except Exception as exc:
                error.append(exc)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        # Poll until the worker finishes; generous outer budget to avoid flakes.
        deadline = time.monotonic() + 10.0
        while t.is_alive() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert not t.is_alive(), "worker thread did not finish within budget"
    finally:
        idle_loop.close()

    assert len(error) == 1
    assert str(error[0]).startswith("sandbox could not start the terminal session:")


def test_the_impty_hook_supplies_the_keepers_own_pty_allocator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A plan whose openpty stayed None would leave the session on imp's OWN
    # devpts, whose nodes do not exist inside the keeper — the unnamed tty this
    # quest exists to fix. An allocator rather than a pre-made fd pair, so the
    # session owns both fds from birth and a failed start cannot leak them.
    ns = _FakeNamespace()
    _stub_ensure_namespace(monkeypatch, ns)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    plan = _hook_from_worker(ctx, ["vim", "notes.txt"])
    assert plan is not None
    assert ns.pty_calls == 0            # planning allocates nothing
    assert plan.openpty is not None
    assert plan.openpty() == (31, 32)   # the NAMESPACE's pty, not pty.openpty()'s
    assert ns.pty_calls == 1


def test_a_failed_pty_allocation_surfaces_as_the_terminal_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # tty-naming AC 11 (this file's older "AC 12" cites a different spec): the
    # allocator runs inside TerminalSession.__init__, DOWNSTREAM of the
    # acquisition wrapper, so only the hook's own closure can frame it. Unframed,
    # the rarest of the three terminal-start failures would be the only one
    # reaching the agent as a bare OSError with no idea what failed.
    from impty.driver import ImptyDriver

    ns = _FakeNamespace(pty_error=OSError(errno.ENOSPC, "No space left on device"))
    _stub_ensure_namespace(monkeypatch, ns)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)

    async def _main() -> str:
        driver = ImptyDriver(sandbox=make_impty_sandbox_hook(ctx))
        try:
            await asyncio.to_thread(lambda: driver.start(["true"]))
            return "no error raised"
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        finally:
            await asyncio.to_thread(driver.close)

    got = asyncio.run(_main())
    assert got.startswith(
        "RuntimeError: sandbox could not start the terminal session: "), got
    assert "No space left on device" in got, got
    assert ns.pty_calls == 1


def test_attaching_the_driver_hooks_it_and_registers_it_in_one_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # One call does both, so the broken pairing is unrepresentable: a driver
    # hooked but not registered would outlive the namespace it spawns into, and a
    # driver registered but not hooked would be closed at teardown for nothing.
    from imp.agents.tools.context import attach_impty_driver, close_impty_session

    ns = _FakeNamespace()
    _stub_ensure_namespace(monkeypatch, ns)
    ctx = _ctx(tmp_path)
    ctx.base_sandbox_policy = _active_policy(tmp_path)
    closed: list[str] = []

    async def _main() -> object:
        driver = attach_impty_driver(ctx, journal_dir=tmp_path)
        assert ctx.impty_driver is driver
        # The hook is live: it plans a join rather than declining.
        plan = await asyncio.to_thread(lambda: driver._sandbox(["true"], None, None))
        assert plan is not None and plan.argv[0] == "nsenter"
        worktree = tmp_path / "worktree"
        ctx.project_root = worktree
        assert driver._cwd_resolver is not None
        assert driver._cwd_resolver(".") == str(worktree)
        raw_absolute = f"{tmp_path}//absolute"
        assert driver._cwd_resolver(raw_absolute) == raw_absolute
        monkeypatch.setattr(driver, "close", lambda: closed.append("closed"))
        return driver

    driver = asyncio.run(_main())
    close_impty_session(ctx)
    assert closed == ["closed"]
    # The DRIVER survives its session: close() clears only the session, so a later
    # start() opens one in whichever namespace is current.
    assert ctx.impty_driver is driver


_JOURNAL_CHILD = (
    "import os, time\n"
    "os.write(1, b'JOURNAL-MARKER\\r\\n')\n"
    "time.sleep(30)\n"
)


@pytest.mark.skipif(not can_pty(), reason="pty not available")
def test_the_attached_driver_journals_into_the_step_dir_and_snapshots_into_the_quest(
    tmp_path: Path,
) -> None:
    from imp.agents.runner import QuestContext
    from imp.agents.tools.context import attach_impty_driver

    quest_dir = tmp_path / "quests" / "q1"
    transcript = quest_dir / "steps" / "implement" / "coder"   # what both providers pass
    transcript.mkdir(parents=True)
    (quest_dir / "artifacts").mkdir()
    ctx = _ctx(tmp_path)
    ctx.quest_context = QuestContext(
        title="T", project_root=tmp_path, workflow_dir=tmp_path, quest_dir=quest_dir)

    async def _main() -> str:
        driver = attach_impty_driver(ctx, journal_dir=transcript)
        try:
            await asyncio.to_thread(
                lambda: driver.start([sys.executable, "-c", _JOURNAL_CHILD]))
            return await asyncio.to_thread(driver.snapshot)
        finally:
            await asyncio.to_thread(driver.close)

    reply = asyncio.run(_main())
    png = Path(reply[len("[wrote "):-1])
    assert png.parent == quest_dir / "snapshots"
    assert png.stat().st_size > 0
    assert b"JOURNAL-MARKER" in (transcript / "pty-1.raw").read_bytes()
    # quest_dir/artifacts/** is the ONLY git-committed location (_harvest_artifacts
    # walks it into refs/imp/artifacts/<id>), so a real drive must leave it untouched.
    assert list((quest_dir / "artifacts").rglob("*")) == []


def test_a_context_with_no_quest_dir_gets_no_snapshot_dir(tmp_path: Path) -> None:
    # A QuestContext with neither quest_dir nor quest_home — a run outside a quest —
    # makes tool_quest_dir yield None. That is the snapshot_dir=None the
    # driver already models (temp-file snapshots), not a new case — and not a raise.
    from imp.agents.runner import QuestContext
    from imp.agents.tools.context import attach_impty_driver, tool_quest_dir

    ctx = _ctx(tmp_path)
    ctx.quest_context = QuestContext(
        title="T", project_root=tmp_path, workflow_dir=tmp_path)
    assert tool_quest_dir(ctx) is None

    async def _main() -> object:
        return attach_impty_driver(ctx, journal_dir=tmp_path)

    driver = asyncio.run(_main())
    assert driver._snapshot_dir is None
    assert [p.name for p in tmp_path.iterdir()] == []


def test_a_failing_pty_close_still_reaps_the_keeper(tmp_path: Path) -> None:
    # The session close runs FIRST (the namespace must not die under a live pty),
    # so an exception there sits directly in front of the keeper close. Both
    # existing ImptyDriver.close() call sites are already guarded, and the
    # providers suppress what this raises — so an unguarded call here would leak
    # a live keeper silently for the rest of the run.
    from imp.agents.tools.context import close_sandbox_namespace

    closed: list[str] = []

    class _Boom:
        def close(self) -> None:
            closed.append("session")
            raise RuntimeError("teardown blew up")

    class _NS:
        def close(self) -> None:
            closed.append("namespace")

    ctx = _ctx(tmp_path)
    ctx.impty_driver = _Boom()
    ctx.sandbox_ns = _NS()
    with pytest.raises(RuntimeError, match="teardown blew up"):
        close_sandbox_namespace(ctx)
    # Ordering intact, keeper reaped anyway, and the failure is NOT swallowed —
    # suppressing it here would hide a real teardown bug behind a silent leak.
    assert closed == ["session", "namespace"]
    assert ctx.sandbox_ns is None


def test_close_impty_session_is_a_no_op_without_a_driver(tmp_path: Path) -> None:
    # Every non-impty run reaches both call sites with impty_driver None, and the
    # conftest owner reaches close_sandbox_namespace for every registered context.
    from imp.agents.tools.context import close_impty_session

    ctx = _ctx(tmp_path)
    assert ctx.impty_driver is None
    close_impty_session(ctx)


def test_both_providers_attach_the_driver_through_the_context() -> None:
    # The wiring is two one-line call sites and no test can drive a provider's
    # _enter, so it is pinned at the source. A bare ImptyDriver() in either
    # provider is the exact regression: it spawns on the host, unconfined.
    from pathlib import Path as _Path

    from imp.providers import claude_code, codex_runner
    for mod in (claude_code, codex_runner):
        src = _Path(mod.__file__).read_text()
        assert "attach_impty_driver(" in src, mod.__name__
        # …and with THIS step's transcript dir, which is where the pty journal lands.
        assert "journal_dir=invocation.transcript_dir" in src, mod.__name__
        assert "ImptyDriver()" not in src, mod.__name__
