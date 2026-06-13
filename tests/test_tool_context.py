# tests/test_tool_context.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.permissions import PermissionDenied
from imp.agents.tools.context import ToolContext
from imp.engine.sandbox import SandboxPolicy
from imp.engine.types import FsRule, SourceLoc


def _allow(ops: tuple[str, ...], paths: tuple[str, ...]) -> FsRule:
    return FsRule(
        operations=ops, paths=paths, mode="allow",  # type: ignore[arg-type]
        source=SourceLoc(file=Path("t.yaml"), line=1),
    )


def test_resolve_project_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    # Paths are now worktree-relative; /project/ prefix is gone.
    assert ctx.resolve_path("src/x.py") == (tmp_path / "src" / "x.py").resolve()



def test_check_fs_permission_passthrough(tmp_path: Path) -> None:
    ctx_with_rule = _ctx(tmp_path, rules=(_allow(("write",), ("**",)),))
    ctx_with_rule.check_fs_permission("write", "x.py")  # ok — rule matches
    ctx_no_rules = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        ctx_no_rules.check_fs_permission("write", "x.py")  # denied — default-deny write


def test_change_context_field(tmp_path: Path) -> None:
    from imp.agents.runner import ChangeContext
    ctx = _ctx(tmp_path)
    assert ctx.change_context is None
    ctx.change_context = ChangeContext(slug="chg-7", title="Test change", project_root=tmp_path,
                                       workflow_dir=tmp_path / ".imp" / "change" / "workflow")
    assert ctx.change_context.project_root == tmp_path


def _ctx(tmp_path: Path, rules: tuple[FsRule, ...] = ()) -> ToolContext:
    return ToolContext(
        project_root=tmp_path,
        fs_permissions=rules,
        events_path=tmp_path / "events.jsonl",
    )


def test_running_processes_starts_empty(tmp_path):
    from imp.agents.tools.context import ToolContext

    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )
    assert ctx.running_processes == []


def test_terminate_running_processes_drains(tmp_path):
    """terminate_running_processes signals SIGTERM, waits, then SIGKILL.
    The registry is cleared regardless of whether the process honored SIGTERM."""
    import anyio

    from imp.agents.tools.context import ToolContext, terminate_running_processes

    ctx = ToolContext(
        project_root=tmp_path,
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
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

    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "e.jsonl")
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

    from imp.agents.tools import context
    from imp.agents.tools.limits import PROCESS_KILL_GRACE_S
    sig = inspect.signature(context.terminate_running_processes)
    assert sig.parameters["timeout"].default == PROCESS_KILL_GRACE_S
    assert PROCESS_KILL_GRACE_S == 4.0


def test_tool_context_has_event_log_field(tmp_path) -> None:
    from imp.agents.tools.context import ToolContext
    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl",
    )
    assert ctx.event_log is None
    sentinel = object()
    ctx.event_log = sentinel
    assert ctx.event_log is sentinel


def test_tool_context_resolved_tiers_defaults_none(tmp_path):
    from imp.agents.tools.context import ToolContext
    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "e.jsonl")
    assert ctx.resolved_tiers is None


def test_tool_context_tiers_defaults_empty(tmp_path) -> None:
    from imp.agents.tools.context import ToolContext
    ctx = ToolContext(
        project_root=tmp_path, fs_permissions=(),
        events_path=tmp_path / "events.jsonl")
    assert ctx.tiers == ()
    ctx.tiers = ("low", "high")
    assert ctx.tiers == ("low", "high")


# --- resolve_path / resolve_path_strict / authorize_fs tests ---

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


def test_resolve_path_strict_rejects_absolute_outside_root(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        ctx.resolve_path_strict("/etc/passwd")


def test_resolve_path_strict_rejects_parent_escape(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        ctx.resolve_path_strict("../../etc/passwd")


def test_resolve_path_strict_normalizes_embedded_traversal(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        ctx.resolve_path_strict("imp/specs/../../../etc/passwd")


def test_resolve_path_strict_maps_relative_under_root(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert ctx.resolve_path_strict("src/foo.py") == (tmp_path / "src" / "foo.py").resolve()


def _active_policy(tmp_path: Path) -> SandboxPolicy:
    return SandboxPolicy(
        active=True,
        worktree=str(tmp_path),
        git_common_dir=str(tmp_path / ".git"),
        git_ro_remask=(),
        writable_caches=(),
        extra_readable=(),
        extra_writable=(),
        secret_dirs=("/home/u/.ssh",),
        secret_files=("/home/u/.netrc",),
        setenv=(),
        network=True,
        note="",
    )


def test_authorize_fs_in_worktree_applies_fs_permissions(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)                       # no rules → default-deny write
    ctx.authorize_fs(("read",), "a.txt")       # default-allow read: ok
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("write",), "a.txt")  # default-deny write
    ctx_w = _ctx(tmp_path, rules=(_allow(("write",), ("**",)),))
    assert ctx_w.authorize_fs(("write",), "a.txt") == (tmp_path / "a.txt").resolve()


def test_authorize_fs_out_of_worktree_unsandboxed_allows(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)                        # sandbox_policy is None → bash parity
    out = tmp_path.parent / "outside.txt"
    assert ctx.authorize_fs(("write",), str(out)) == out.resolve()
    assert ctx.authorize_fs(("read",), str(out)) == out.resolve()


def test_authorize_fs_out_of_worktree_sandboxed_read(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.sandbox_policy = _active_policy(tmp_path)
    # secret-masked read denied; ordinary out-of-worktree read allowed
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("read",), "/home/u/.ssh/id_rsa")
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("read",), "/home/u/.netrc")
    assert ctx.authorize_fs(("read",), "/usr/share/x") == Path("/usr/share/x")


def test_authorize_fs_out_of_worktree_sandboxed_write(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.sandbox_policy = _active_policy(tmp_path)
    assert ctx.authorize_fs(("write",), "/tmp/scratch.txt") == Path("/tmp/scratch.txt")
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("write",), "/usr/lib/x")          # no writable bind
    with pytest.raises(PermissionDenied):
        ctx.authorize_fs(("write",), "/home/u/.ssh/id_rsa")  # secret mask


def test_background_registry_and_bg_ids(tmp_path) -> None:
    from imp.agents.tools.context import ToolContext
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    assert ctx.background_processes == {}
    assert ctx.next_bg_id() == "bg1"
    assert ctx.next_bg_id() == "bg2"
    # distinct contexts do not share the counter
    ctx2 = ToolContext(project_root=tmp_path, fs_permissions=(),
                       events_path=tmp_path / "events.jsonl")
    assert ctx2.next_bg_id() == "bg1"


def test_get_parallel_limiter_is_lazy_and_per_invocation(tmp_path: Path) -> None:
    """The limiter is created once on first call (identity stable across calls),
    its width equals the resolved knob, and a SEPARATE ToolContext gets its OWN
    limiter (non-inheritance / per-invocation isolation — the nested-pool
    deadlock guard)."""
    from imp.agents.parallel import max_parallel_tasks, reset_parallelism_cache
    from imp.agents.tools.context import ToolContext

    reset_parallelism_cache()
    ctx = ToolContext(project_root=tmp_path, fs_permissions=(),
                      events_path=tmp_path / "events.jsonl")
    assert ctx.parallel_limiter is None        # not created until asked
    lim = ctx.get_parallel_limiter()
    assert lim is ctx.get_parallel_limiter()   # check-then-set, single object
    assert lim.total_tokens == max_parallel_tasks()
    other = ToolContext(project_root=tmp_path, fs_permissions=(),
                        events_path=tmp_path / "events.jsonl")
    assert other.get_parallel_limiter() is not lim  # fresh limiter per invocation
    reset_parallelism_cache()
