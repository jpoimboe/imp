from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _phase(tree: nv.NarrativeTree, name: str) -> nv.PhaseNode:
    for n in tree.root.children:
        if isinstance(n, nv.PhaseNode) and n.name == name:
            return n
    raise AssertionError(f"no phase {name!r}")


def _step(phase: nv.PhaseNode, name: str) -> nv.StepNode:
    for n in phase.children:
        if isinstance(n, nv.StepNode) and n.name == name:
            return n
    raise AssertionError(f"no step {name!r}")


def test_chrome_lands_on_step_and_phase_nodes() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="scoping", kind="agent",
           phase="front-door", chrome="none", model="m", provider="anthropic"),
        ev("3", "step_completed", step="scoping", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    ph = _phase(tree, "front-door")
    assert ph.chrome == "none"
    assert _step(ph, "scoping").chrome == "none"


def test_chrome_absent_leaves_nodes_none() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-writer", kind="agent",
           phase="spec", model="m", provider="anthropic"),
        ev("3", "step_completed", step="spec-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    ph = _phase(tree, "spec")
    assert ph.chrome is None
    assert _step(ph, "spec-writer").chrome is None
