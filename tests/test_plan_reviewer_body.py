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


def test_plan_reviewer_has_two_major_sharpenings() -> None:
    low = _body().lower()
    assert "non-trivial risk" in low            # probabilistic defects are Major
    assert "behavioral impact" in low and "not file type" in low


def test_plan_reviewer_softened_decisions_handling() -> None:
    low = _body().lower()
    # A user-confirmed decision contradicting an explicit spec AC is Major.
    assert "acceptance criterion" in low
    assert "regardless" in low


def test_plan_reviewer_reads_only_newest_plan_and_spec() -> None:
    low = _body().lower()
    assert "do not read prior or future" in low


def test_plan_reviewer_preserves_decisions_brief_and_complete_step() -> None:
    body = _body()
    assert "Decisions section" in body
    assert "during planning" in body
    assert "complete_step(" in body
    assert body.count("complete_step(") >= 3   # the three example calls
