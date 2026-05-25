from pathlib import Path

from imp.engine.change import Change
from imp.engine.state import State
from tests.conftest import write_agent_md


def _seed_workflow(project: Path) -> None:
    src = project / ".imp" / "workflows" / "default"
    (src / "agents").mkdir(parents=True)
    write_agent_md(
        src / "agents" / "stub.md",
        frontmatter=(
            "name: stub\ntools: [complete_step]\n"
            "output:\n  status: {type: enum, values: [Success, Error]}"
        ),
    )
    (src / "workflow.yaml").write_text(
        "name: t\ndescription: \"\"\nphases: [p]\n"
        "p:\n  steps:\n    - {name: s, file: agents/stub.md}\n"
    )
    (project / ".imp" / "config.yaml").write_text(
        "roles:\n  default: {prefer: [{provider: anthropic, model: x}]}\n"
    )


def test_create_change_allocates_id_1(tmp_path: Path) -> None:
    _seed_workflow(tmp_path)
    ch = Change.create(tmp_path, title="first")
    assert ch.id == 1
    assert (tmp_path / ".imp" / "changes" / "1" / "workflow" / "workflow.yaml").exists()
    assert (tmp_path / ".imp" / "changes" / "1" / "change.json").exists()
    state = State.load(tmp_path / ".imp" / "changes" / "1" / "state")
    assert state.phase == "p"
    assert state.step is None


def test_create_change_increments(tmp_path: Path) -> None:
    _seed_workflow(tmp_path)
    a = Change.create(tmp_path, title="a")
    b = Change.create(tmp_path, title="b")
    assert (a.id, b.id) == (1, 2)


def test_snapshot_is_isolated(tmp_path: Path) -> None:
    _seed_workflow(tmp_path)
    ch = Change.create(tmp_path, title="x")
    # Mutate the live workflow; snapshot should not change.
    (tmp_path / ".imp" / "workflows" / "default" / "workflow.yaml").write_text("BROKEN")
    snap = ch.workflow_dir / "workflow.yaml"
    assert "phases: [p]" in snap.read_text()


def test_load_existing_change(tmp_path: Path) -> None:
    _seed_workflow(tmp_path)
    a = Change.create(tmp_path, title="x")
    b = Change.load(tmp_path, a.id)
    assert b.id == a.id
    assert b.title == "x"
