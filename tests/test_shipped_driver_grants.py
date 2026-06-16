from imp.assets import default_workflow_dir
from imp.engine.loader import parse_agent_file


def _drivers(name: str) -> frozenset[str]:
    path = default_workflow_dir() / "agents" / f"{name}.md"
    return parse_agent_file(path).drivers


def test_spec_writer_and_finalize_hold_impty() -> None:
    assert _drivers("spec-writer") == frozenset({"impty"})
    assert _drivers("finalize") == frozenset({"impty"})


def test_only_granted_agents_hold_drivers() -> None:
    agents_dir = default_workflow_dir() / "agents"
    granted = {"spec-writer", "finalize"}
    for path in sorted(agents_dir.glob("*.md")):
        name = path.stem
        if name in granted:
            continue
        assert parse_agent_file(path).drivers == frozenset(), (
            f"{name}.md should have no drivers"
        )
