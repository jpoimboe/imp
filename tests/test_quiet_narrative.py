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


def test_forward_goto_falls_back_to_raw_target_when_a_different_step_intervenes() -> None:
    # Defensive fallback (narrative.py): the forward goto normally resolves its
    # arrow from the TARGET step's phase, since the target runs next. If some
    # OTHER step's step_started arrives first (never the shipped path), fall back
    # to the raw target name rather than mislabelling with the intervening phase.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="fast-path-spec-gate", kind="branch",
           phase="spec", quiet=True),
        ev("3", "step_completed", step="fast-path-spec-gate", kind="branch",
           action={"verb": "goto", "target": "plan-writer", "resume": False},
           var_mutations={}),
        # A different step runs next, in an unrelated phase — not the goto target.
        ev("4", "step_started", step="interloper", kind="agent", phase="elsewhere",
           model="m", provider="anthropic"),
    ])
    assert _steps(tree)["fast-path-spec-gate"].quiet_line == "→ plan-writer"


def test_same_phase_forward_goto_renders_nothing() -> None:
    # A quiet gate that forward-gotos a step in its OWN phase (e.g. panel-gate →
    # recheck, both in review) is an internal skip, not a phase transition: it
    # resolves to the silent "" sentinel (not a redundant "→ review" arrow), so
    # nothing renders.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="panel-gate", kind="branch",
           phase="review", quiet=True),
        ev("3", "step_completed", step="panel-gate", kind="branch",
           action={"verb": "goto", "target": "recheck", "resume": False},
           var_mutations={}),
        ev("4", "step_started", step="recheck", kind="agent", phase="review",
           model="m", provider="anthropic"),
    ])
    assert _steps(tree)["panel-gate"].quiet_line == ""


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


def _breadcrumb(node: nv.StepNode) -> nv.BreadcrumbLine:
    return next(c for c in node.children if isinstance(c, nv.BreadcrumbLine))


def test_loud_forward_goto_route_resolves_to_target_phase() -> None:
    # spec-reviewer (loud, spec phase) clean-approves with a plain forward goto to
    # plan-writer (plan phase, no feedback). The breadcrumb route names the TARGET
    # phase, known only once plan-writer's step_started arrives.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="spec-reviewer", kind="agent", phase="spec",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="spec-reviewer", kind="agent",
           action={"verb": "goto", "target": "plan-writer",
                   "resume": False, "rework": False},
           var_mutations={}),
        ev("4", "step_started", step="plan-writer", kind="agent", phase="plan",
           model="m", provider="anthropic"),
    ])
    node = _steps(tree)["spec-reviewer"]
    assert _breadcrumb(node).route == "→ plan"
    assert node.quiet_line is None        # a loud step does not touch quiet_line


def test_loud_same_phase_forward_goto_route_is_silent() -> None:
    # review (loud, review phase) clean-approves with a forward goto to
    # integrate-rebase, which is ALSO in the review phase: an internal skip, not a
    # phase transition -> the resolved-but-silent "" sentinel (no arrow).
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="review", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="review", kind="agent",
           action={"verb": "goto", "target": "integrate-rebase",
                   "resume": False, "rework": False},
           var_mutations={}),
        ev("4", "step_started", step="integrate-rebase", kind="script",
           phase="review", quiet=True),
    ])
    assert _breadcrumb(_steps(tree)["review"]).route == ""


def test_loud_reviewer_rework_via_plain_goto_resolves_to_reworking() -> None:
    # A loud reviewer rework is a plain goto to an earlier phase carrying feedback
    # (rework=True), NOT a resume edge. It must read as "→ reworking" immediately,
    # never deferring to name the earlier phase.
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="plan-review", kind="agent", phase="plan",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="plan-review", kind="agent",
           action={"verb": "goto", "target": "plan-writer",
                   "resume": False, "rework": True},
           var_mutations={}),
    ])
    assert _breadcrumb(_steps(tree)["plan-review"]).route == "→ reworking"


def test_loud_resume_edge_resolves_to_reworking() -> None:
    # A true resume edge (integration-review -> integrator) reads "→ reworking".
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="integration-review", kind="agent",
           phase="review", model="m", provider="anthropic"),
        ev("3", "step_completed", step="integration-review", kind="agent",
           action={"verb": "goto", "target": "integrator",
                   "resume": True, "rework": True},
           var_mutations={}),
    ])
    assert _breadcrumb(_steps(tree)["integration-review"]).route == "→ reworking"


def test_loud_continue_has_no_route() -> None:
    tree = nv.build_tree([
        ev("1", "change_created", change_id=1, title="x"),
        ev("2", "step_started", step="summarizer", kind="agent", phase="review",
           model="m", provider="anthropic"),
        ev("3", "step_completed", step="summarizer", kind="agent",
           action={"verb": "continue"}, var_mutations={}),
    ])
    assert _breadcrumb(_steps(tree)["summarizer"]).route is None
