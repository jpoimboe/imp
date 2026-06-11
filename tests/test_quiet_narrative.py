from __future__ import annotations

from imp.cli import narrative as nv
from tests._event_fixtures import ev


def _steps(tree: nv.NarrativeTree) -> dict[str, nv.StepNode]:
    found: dict[str, nv.StepNode] = {}

    def walk(nodes: list) -> None:
        for n in nodes:
            if isinstance(n, nv.StepNode):
                found[n.name] = n
            if isinstance(n, (nv.PhaseNode, nv.StepNode)):
                walk(n.children)
    walk(tree.root.children)
    return found


def test_quiet_flag_lands_on_the_step_node() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="commit-check", kind="script",
           phase="implement", quiet=True),
        ev("3", "step_completed", step="commit-check", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ])
    node = _steps(tree)["commit-check"]
    assert node.quiet is True
    assert node.quiet_line is None        # a linear continue → no routing line


def test_resume_edge_resolves_to_reworking() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-verify-commit", kind="script",
           phase="spec", quiet=True),
        ev("3", "step_completed", step="spec-verify-commit", kind="script",
           action={"verb": "goto", "target": "spec-writer", "resume": True},
           var_mutations={}),
    ])
    assert _steps(tree)["spec-verify-commit"].quiet_line == "→ reworking"


def test_forward_goto_resolves_to_the_target_phase() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="fast-path-spec-gate", kind="branch",
           phase="spec", quiet=True),
        ev("3", "step_completed", step="fast-path-spec-gate", kind="branch",
           action={"verb": "goto", "target": "plan-writer", "resume": False},
           var_mutations={}),
        # the goto target runs next, in the plan phase — that is where the
        # arrow's phase name comes from.
        ev("4", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
    ])
    assert _steps(tree)["fast-path-spec-gate"].quiet_line == "→ plan"


def test_loud_step_has_no_quiet_line() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-writer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="spec-writer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    node = _steps(tree)["spec-writer"]
    assert node.quiet is False
    assert node.quiet_line is None
