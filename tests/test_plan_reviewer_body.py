from __future__ import annotations

from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow


def _body() -> str:
    wf = load_workflow(bundled_workflow_dir("imp"))
    return wf.agents["plan-reviewer"].prompt_template_source


def test_plan_reviewer_is_code_first() -> None:
    low = _body().lower()
    assert "like a diff" in low
    assert "trust nothing" in low


def test_plan_reviewer_boundary_list_includes_ordering_and_emit_timing() -> None:
    low = _body().lower()
    assert "ordering" in low and "sequencing" in low
    assert "emit" in low  # stateful emit / un-emit timing hedge


def test_plan_reviewer_references_canonical_rubric() -> None:
    assert '{% include "partials/severity.md" %}' in _body()


def test_canonical_rubric_carries_two_major_sharpenings() -> None:
    partial = (bundled_workflow_dir("imp") / "partials" / "severity.md").read_text().lower()
    assert "non-trivial risk" in partial        # probabilistic defects are Major
    assert "behavioral impact" in partial and "not file type" in partial


def test_plan_reviewer_routes_recorded_scope_deviations_to_gate() -> None:
    body = _body()
    low = body.lower()
    # A scope-contradicting decision still hinges on an explicit spec AC,
    assert "acceptance criterion" in low
    # but a recorded deviation rides to the gate non-blocking,
    assert "needs-decision" in low
    assert "non-blocking" in low
    # while an unrecorded sneak-by still blocks Major.
    assert "sneak-by" in low


def test_plan_reviewer_reads_only_newest_plan_and_spec() -> None:
    low = _body().lower()
    assert "do not read prior or future" in low


def test_plan_reviewer_preserves_decisions_brief_and_complete_step() -> None:
    body = _body()
    assert "Decisions section" in body
    assert "during planning" in body
    assert "complete_step(" in body
    assert body.count("complete_step(") >= 3   # the three example calls
