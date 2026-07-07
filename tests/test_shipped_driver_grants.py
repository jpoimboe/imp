from imp.assets import bundled_workflow_dir
from imp.engine.loader import parse_agent_file


def _drivers(name: str) -> frozenset[str]:
    path = bundled_workflow_dir("imp") / "agents" / f"{name}.md"
    return parse_agent_file(path).drivers


def test_spec_phase_agents_hold_impty_and_finalize_holds_none() -> None:
    assert _drivers("spec-finalizer") == frozenset({"impty"})
    assert _drivers("threshold") == frozenset({"impty"})
    assert _drivers("finalize") == frozenset()


def test_only_granted_agents_hold_drivers() -> None:
    agents_dir = bundled_workflow_dir("imp") / "agents"
    granted = {"spec-finalizer", "threshold"}
    for path in sorted(agents_dir.glob("*.md")):
        name = path.stem
        if name in granted:
            continue
        assert parse_agent_file(path).drivers == frozenset(), (
            f"{name}.md should have no drivers"
        )
