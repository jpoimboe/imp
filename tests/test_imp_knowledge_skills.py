"""The grounding skills (imp-knowledge, imp-introspection, imp-memory) ship in the
default workflow, are loadable via the skills mechanism with no frontmatter
(Profile B), and cover the spec-mandated topics verified against live code."""

from __future__ import annotations

import pytest

from imp.agents.skills import SkillNotFound, load_agent_skill
from imp.assets import bundled_workflow_dir


def _skills_dir():
    return bundled_workflow_dir("imp") / "skills"


def test_knowledge_skill_is_loadable_profile_b() -> None:
    body = load_agent_skill(_skills_dir(), "imp-knowledge")
    assert body.strip(), "imp-knowledge is empty"
    assert not body.lstrip().startswith("---"), "imp-knowledge has frontmatter"


def test_knowledge_lists_status_set_and_draft_relabel() -> None:
    body = load_agent_skill(_skills_dir(), "imp-knowledge").lower()
    for status in ("new", "running", "paused", "saved", "completed",
                   "merge_blocked", "abandoned"):
        assert status in body, status
    assert "errored" not in body
    assert "draft" in body


def test_knowledge_names_the_three_left_draft_dispositions() -> None:
    body = load_agent_skill(_skills_dir(), "imp-knowledge")
    assert "discard_conversation" in body
    assert "save_to_backlog" in body
    assert "archive_conversation" in body


def test_knowledge_covers_home_zones_materialize_and_vocabulary() -> None:
    body = load_agent_skill(_skills_dir(), "imp-knowledge").lower()
    assert ".imp/quests/" in body
    assert "backlog" in body
    assert "materialize" in body
    for term in ("spec", "plan", "phase", "step", "lane"):
        assert term in body, term


def test_introspection_skill_is_loadable_profile_b() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspection")
    assert body.strip(), "imp-introspection is empty"
    assert not body.lstrip().startswith("---"), "imp-introspection has frontmatter"


def test_introspection_names_ground_truth_files() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspection")
    assert "workflow/workflow.yaml" in body
    assert "state/state.json" in body


def test_introspection_names_the_read_commands() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspection")
    assert "imp help" in body
    assert "imp list" in body
    assert "imp list --all" in body
    assert "imp search" in body
    assert "imp show <id> [spec|plan|events|status]" in body


def test_introspection_documents_the_three_tier_split() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspection").lower()
    # narrative / digest / raw are the three named timeline tiers
    for tier in ("narrative", "digest", "raw"):
        assert tier in body, tier
    # narrative tier names imp log; digest tier names both front doors
    assert "imp log <id>" in body
    assert "imp show <id> events" in body


def test_introspection_documents_the_cross_change_query() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspection")
    # in-loop cross-change front door, keyword-form as an agent would call it
    assert "query_events(quest_id=" in body
    # bash cross-change front door
    assert "imp show <id> events" in body


def test_introspection_documents_bounded_output_techniques() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspection")
    # scope-the-query flags, in both CLI and tool spelling
    for flag in ("--limit", "--type", "--agent"):
        assert flag in body, flag
    for kw in ("`limit`", "`type`", "`agent_id`"):
        assert kw in body, kw
    # scan-then-zoom: raw tier is opt-in, in both spellings
    assert "--full" in body
    assert "full=True" in body


def test_introspection_search_entry_spans_all_zones_and_content() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspection")
    low = body.lower()
    # imp search now spans active + backlog + archive, not archive-only.
    for zone in ("active", "backlog", "archive"):
        assert zone in low, zone
    # It matches conversation content, not just id/title.
    assert "conversation content" in low
    # Composable zone flags and the terse -l list are documented.
    for flag in ("--active", "--backlog", "--archived", "-l"):
        assert flag in body, flag
    # The stale archive-only framing is gone.
    assert "search the archive (completed, abandoned, and left" not in body


def test_memory_skill_is_loadable_profile_b() -> None:
    body = load_agent_skill(_skills_dir(), "imp-memory")
    assert body.strip(), "imp-memory is empty"
    assert not body.lstrip().startswith("---"), "imp-memory has frontmatter"


def test_memory_covers_two_tiers_of_durable_state() -> None:
    body = load_agent_skill(_skills_dir(), "imp-memory")
    low = body.lower()
    # two-tiers framing
    assert "two tiers" in low
    # per-quest records tier
    assert ".imp/quests/" in body
    # project-config tier
    assert "imp/project.yaml" in body
    assert "guidance" in low
    assert "facts" in low
    assert "default-workflow" in body
    assert "test_cmd_fast" in body
    assert "test_cmd_slow" in body
    # the concrete config layering chain
    assert "floor ← imp/project.yaml ← ~/.config/imp/config.yaml ← .imp/config.yaml" in body
    # how to read what is active, incl. imp config's blind spots
    assert "imp config" in body
    assert "blind spots" in body


def test_knowledge_maps_to_both_spokes() -> None:
    body = load_agent_skill(_skills_dir(), "imp-knowledge")
    assert 'load_skill(name="imp-introspection")' in body
    assert 'load_skill(name="imp-memory")' in body


def test_old_skill_names_are_gone() -> None:
    for old in ("imp-mental-model", "imp-introspect"):
        with pytest.raises(SkillNotFound):
            load_agent_skill(_skills_dir(), old)
