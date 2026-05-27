# tests/test_tool_context.py
from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.permissions import PermissionDenied
from imp.agents.tools.context import ToolContext
from imp.engine.types import FsRule, SourceLoc


def _allow(ops: tuple[str, ...], paths: tuple[str, ...]) -> FsRule:
    return FsRule(
        operations=ops, paths=paths, mode="allow",  # type: ignore[arg-type]
        source=SourceLoc(file=Path("t.yaml"), line=1),
    )


def test_resolve_project_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert ctx.resolve_path("/project/src/x.py") == tmp_path / "src" / "x.py"


def test_resolve_artifacts_path(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    assert ctx.resolve_path("/artifacts/spec.md") == ctx.artifacts_dir / "spec.md"


def test_resolve_scratch_returns_marker(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    out = ctx.resolve_path("/scratch/notes.md")
    assert out is None  # marker: caller goes through ctx.scratch dict


def test_scratch_read_write(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    ctx.scratch["/scratch/notes"] = "hello"
    assert ctx.scratch["/scratch/notes"] == "hello"


def test_unknown_prefix_raises(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="virtual path"):
        ctx.resolve_path("/elsewhere/x")


def test_check_fs_permission_passthrough(tmp_path: Path) -> None:
    ctx = _ctx(tmp_path, rules=(_allow(("write",), ("/artifacts/**",)),))
    ctx.check_fs_permission("write", "/artifacts/spec.md")  # ok
    with pytest.raises(PermissionDenied):
        ctx.check_fs_permission("write", "/project/x.py")


def test_change_context_field(tmp_path: Path) -> None:
    from imp.agents.runner import ChangeContext
    ctx = _ctx(tmp_path)
    assert ctx.change_context is None
    ctx.change_context = ChangeContext(change_id=7, project_root=tmp_path, artifacts_dir=tmp_path)
    assert ctx.change_context.change_id == 7


def _ctx(tmp_path: Path, rules: tuple[FsRule, ...] = ()) -> ToolContext:
    artifacts = tmp_path / "_artifacts"
    artifacts.mkdir()
    return ToolContext(
        project_root=tmp_path,
        artifacts_dir=artifacts,
        fs_permissions=rules,
        events_path=tmp_path / "events.jsonl",
    )


def test_running_processes_starts_empty(tmp_path):
    from imp.agents.tools.context import ToolContext

    ctx = ToolContext(
        project_root=tmp_path, artifacts_dir=tmp_path,
        fs_permissions=(), events_path=tmp_path / "events.jsonl",
    )
    assert ctx.running_processes == []


def test_terminate_running_processes_drains(tmp_path):
    """terminate_running_processes signals SIGTERM, waits, then SIGKILL.
    The registry is cleared regardless of whether the process honored SIGTERM."""
    import anyio

    from imp.agents.tools.context import ToolContext, terminate_running_processes

    ctx = ToolContext(
        project_root=tmp_path, artifacts_dir=tmp_path,
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
