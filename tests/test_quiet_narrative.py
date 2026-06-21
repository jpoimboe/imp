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
    assert _steps(tree)["commit-check"].quiet is True


def test_route_label_lands_on_target_step_node() -> None:
    # The rule's label rides the NEXT step's step_started.payload.route_label onto
    # that target StepNode, where the renderer paints it as the yellow header suffix.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="code-reviewer", kind="agent",
           phase="implement", model="m", provider="anthropic"),
        ev("3", "step_completed", step="code-reviewer", kind="agent",
           action={"verb": "goto", "target": "task-lead",
                   "resume": False, "rework": True},
           var_mutations={}, label="addressing review feedback"),
        ev("4", "step_started", step="task-lead", kind="agent",
           phase="implement", model="opus", provider="anthropic",
           route_label="addressing review feedback"),
    ])
    assert _steps(tree)["task-lead"].route_label == "addressing review feedback"


def test_forward_step_without_route_label_has_none() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-reviewer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="spec-reviewer", kind="agent",
           action={"verb": "goto", "target": "plan-writer",
                   "resume": False, "rework": False},
           var_mutations={}, label=None),
        ev("4", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
    ])
    assert _steps(tree)["plan-writer"].route_label is None
