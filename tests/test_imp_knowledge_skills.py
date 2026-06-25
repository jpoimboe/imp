"""The two grounding skills (imp-mental-model, imp-introspect) ship in the
default workflow, are loadable via the skills mechanism with no frontmatter
(Profile B), and cover the spec-mandated topics verified against live code."""

from __future__ import annotations

from imp.agents.skills import load_agent_skill
from imp.assets import bundled_workflow_dir


def _skills_dir():
    return bundled_workflow_dir("imp") / "skills"


def test_mental_model_skill_is_loadable_profile_b() -> None:
    body = load_agent_skill(_skills_dir(), "imp-mental-model")
    assert body.strip(), "imp-mental-model is empty"
    assert not body.lstrip().startswith("---"), "imp-mental-model has frontmatter"
    assert "superpowers" not in body.lower()


def test_mental_model_lists_status_set_and_draft_relabel() -> None:
    body = load_agent_skill(_skills_dir(), "imp-mental-model").lower()
    for status in ("new", "running", "paused", "saved", "completed",
                   "merge_blocked", "abandoned"):
        assert status in body, status
    assert "errored" not in body
    assert "draft" in body


def test_mental_model_names_the_three_left_draft_dispositions() -> None:
    body = load_agent_skill(_skills_dir(), "imp-mental-model")
    assert "discard_conversation" in body
    assert "save_to_backlog" in body
    assert "archive_conversation" in body


def test_mental_model_covers_home_zones_materialize_and_vocabulary() -> None:
    body = load_agent_skill(_skills_dir(), "imp-mental-model").lower()
    assert ".imp/changes/" in body
    assert "backlog" in body
    assert "materialize" in body
    for term in ("spec", "plan", "phase", "step", "lane"):
        assert term in body, term


def test_introspect_skill_is_loadable_profile_b() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspect")
    assert body.strip(), "imp-introspect is empty"
    assert not body.lstrip().startswith("---"), "imp-introspect has frontmatter"
    assert "superpowers" not in body.lower()


def test_introspect_names_ground_truth_files() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspect")
    assert "workflow/workflow.yaml" in body
    assert "state/state.json" in body


def test_introspect_names_the_read_commands() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspect")
    assert "imp help" in body
    assert "imp list" in body
    assert "imp list --all" in body
    assert "imp search" in body
    assert "imp show <id> [spec|plan|events|status|review]" in body
