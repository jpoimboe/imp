from imp.agents.tools.context import ToolContext


def _ctx(tmp_path):
    return ToolContext(project_root=tmp_path,events_path=tmp_path / "e.jsonl")


def test_absolute_path_resolves_without_virtual_namespace(tmp_path) -> None:
    # There is no virtual namespace: a bare absolute path resolves to its literal
    # host location (bounded later by authorize_fs), never a fabricated mount.
    ctx = _ctx(tmp_path)
    from pathlib import Path
    assert ctx.resolve_path("/artifacts/spec.md") == Path("/artifacts/spec.md")
