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


def _breadcrumb(node: nv.StepNode) -> nv.BreadcrumbLine:
    return next(c for c in node.children if isinstance(c, nv.BreadcrumbLine))


def test_quiet_flag_lands_on_the_step_node() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="commit-check", kind="script",
           phase="implement", quiet=True),
        ev("3", "step_completed", step="commit-check", kind="script",
           action={"verb": "continue"}, var_mutations={}),
    ])
    assert _steps(tree)["commit-check"].quiet is True


def test_workflow_route_label_lands_on_the_breadcrumb() -> None:
    # The rule's `label` rides step_completed.payload.label onto the breadcrumb,
    # where the renderer turns it into the inline yellow "→ <label>" that rides
    # on the same completion line.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="code-reviewer", kind="agent",
           phase="implement", model="m", provider="anthropic"),
        ev("3", "step_completed", step="code-reviewer", kind="agent",
           action={"verb": "goto", "target": "task-lead",
                   "resume": False, "rework": True},
           var_mutations={}, label="addressing review feedback"),
    ])
    assert _breadcrumb(_steps(tree)["code-reviewer"]).label == "addressing review feedback"


def test_unlabeled_goto_has_no_route_label() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-reviewer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="spec-reviewer", kind="agent",
           action={"verb": "goto", "target": "plan-writer",
                   "resume": False, "rework": False},
           var_mutations={}, label=None),
    ])
    assert _breadcrumb(_steps(tree)["spec-reviewer"]).label is None
