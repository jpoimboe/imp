from imp.assets import bundled_workflow_dir
from imp.engine.loader import parse_agent_file

HOLDERS = {"batch-code-reviewer", "threshold", "finalize", "spiker"}


def _drivers(name: str) -> frozenset[str]:
    path = bundled_workflow_dir("imp") / "agents" / f"{name}.md"
    return parse_agent_file(path).drivers


def test_the_four_holders_each_hold_only_impty() -> None:
    for name in sorted(HOLDERS):
        assert _drivers(name) == frozenset({"impty"}), (
            f"{name}.md should declare drivers: [impty]"
        )


def test_named_non_holders_hold_no_drivers() -> None:
    for name in ("spec-finalizer", "coder", "code-reviewer"):
        assert _drivers(name) == frozenset(), (
            f"{name}.md should have no drivers"
        )


def test_no_agent_outside_the_holder_set_holds_drivers() -> None:
    agents_dir = bundled_workflow_dir("imp") / "agents"
    for path in sorted(agents_dir.glob("*.md")):
        name = path.stem
        if name in HOLDERS:
            continue
        assert parse_agent_file(path).drivers == frozenset(), (
            f"{name}.md should have no drivers"
        )
