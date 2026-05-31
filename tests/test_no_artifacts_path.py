import pytest

from imp.agents.tools.context import ToolContext


def _ctx(tmp_path):
    return ToolContext(project_root=tmp_path, fs_permissions=(), events_path=tmp_path / "e.jsonl")


def test_artifacts_path_rejected(tmp_path) -> None:
    ctx = _ctx(tmp_path)
    with pytest.raises(ValueError, match="/project/"):
        ctx.resolve_path("/artifacts/spec.md")


def test_project_path_resolves(tmp_path) -> None:
    assert _ctx(tmp_path).resolve_path("/project/a.py") == tmp_path / "a.py"
