"""CompositeBackend routing: /artifacts/, /project/, / fall-back to state."""

from __future__ import annotations

from pathlib import Path

import pytest
from deepagents.backends import StateBackend  # type: ignore[import-untyped]

from imp.agents.backends import build_backend
from imp.agents.runner import AgentInvocation
from imp.engine.types import AgentDef, OutputField, OutputSchema, SourceLoc


def _inv(tmp_path: Path) -> AgentInvocation:
    art = tmp_path / "artifacts"
    art.mkdir()
    proj = tmp_path / "project"
    proj.mkdir()
    (proj / "README.md").write_text("project readme\n")
    agent_def = AgentDef(
        name="x", description="", tools=frozenset(), subagents=(),
        output_schema=OutputSchema(
            fields=(OutputField(name="status", type="enum", values=("Success",)),),
            source=SourceLoc(file=tmp_path / "x.md", line=1),
        ),
        prompt_template_source="",
        source_path=tmp_path / "x.md",
        source=SourceLoc(file=tmp_path / "x.md", line=1),
    )
    return AgentInvocation(
        agent_def=agent_def, model_role="default", change_id=1,
        change_artifacts_dir=art, project_root=proj, interactive=False,
    )


def test_backend_writes_artifacts_to_disk(tmp_path: Path) -> None:
    inv = _inv(tmp_path)
    backend = build_backend(inv)
    backend.write("/artifacts/spec.md", "the spec\n")
    on_disk = (inv.change_artifacts_dir / "spec.md").read_text()
    assert on_disk == "the spec\n"


def test_backend_reads_from_project(tmp_path: Path) -> None:
    inv = _inv(tmp_path)
    backend = build_backend(inv)
    result = backend.read("/project/README.md")
    # FilesystemBackend.read returns ReadResult; content lives in file_data.
    assert result.error is None
    assert result.file_data["content"] == "project readme\n"


def test_state_paths_do_not_leak(tmp_path: Path) -> None:
    """Paths outside /artifacts/ and /project/ route to StateBackend.

    StateBackend requires a LangGraph runtime context to actually persist
    (it lives in the deepagent graph state), so we can't round-trip the
    write here. What we verify is the routing decision: the write attempt
    raises the LangGraph-required RuntimeError (proving it was dispatched
    to StateBackend, not FilesystemBackend), and nothing leaks to disk.
    """
    inv = _inv(tmp_path)
    backend = build_backend(inv)
    with pytest.raises(RuntimeError, match="LangGraph"):
        backend.write("/scratch.txt", "ephemeral")
    assert not (inv.project_root / "scratch.txt").exists()
    assert not (inv.change_artifacts_dir / "scratch.txt").exists()
    assert isinstance(backend.default, StateBackend)
