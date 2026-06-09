import pytest

from imp.agents.permissions import PermissionDenied
from imp.agents.tools.context import ToolContext


def _ctx(tmp_path):
    return ToolContext(project_root=tmp_path, fs_permissions=(), events_path=tmp_path / "e.jsonl")


def test_artifacts_path_rejected(tmp_path) -> None:
    # Absolute paths escape the worktree root and are denied — there is no
    # virtual namespace, so a bare `/artifacts/...` cannot silently resolve.
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        ctx.resolve_path("/artifacts/spec.md")


def test_project_path_rejected(tmp_path) -> None:
    # `/project` is gone: it resolves as an ordinary absolute path, which
    # escapes the worktree root and is denied (Part A — worktree-relative model).
    ctx = _ctx(tmp_path)
    with pytest.raises(PermissionDenied):
        ctx.resolve_path("/project/a.py")
