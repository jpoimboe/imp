from imp.assets import bundled_workflow_dir
from imp.engine.loader import parse_agent_file


def _drivers(name: str) -> frozenset[str]:
    path = bundled_workflow_dir("imp") / "agents" / f"{name}.md"
    return parse_agent_file(path).drivers


def test_spec_writer_finalize_and_front_door_hold_impty() -> None:
    assert _drivers("spec-writer") == frozenset({"impty"})
    assert _drivers("finalize") == frozenset({"impty"})
    assert _drivers("front-door") == frozenset({"impty"})


def test_only_granted_agents_hold_drivers() -> None:
    agents_dir = bundled_workflow_dir("imp") / "agents"
    granted = {"spec-writer", "finalize", "front-door"}
    for path in sorted(agents_dir.glob("*.md")):
        name = path.stem
        if name in granted:
            continue
        assert parse_agent_file(path).drivers == frozenset(), (
            f"{name}.md should have no drivers"
        )
