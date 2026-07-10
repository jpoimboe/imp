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
    assert "imp show <id> [spec|plan|events|status]" in body


def test_introspect_documents_the_three_tier_split() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspect").lower()
    # narrative / digest / raw are the three named timeline tiers
    for tier in ("narrative", "digest", "raw"):
        assert tier in body, tier
    # narrative tier names imp log; digest tier names both front doors
    assert "imp log <id>" in body
    assert "imp show <id> events" in body


def test_introspect_documents_the_cross_change_query() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspect")
    # in-loop cross-change front door, keyword-form as an agent would call it
    assert "query_events(quest_id=" in body
    # bash cross-change front door
    assert "imp show <id> events" in body


def test_introspect_documents_bounded_output_techniques() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspect")
    # scope-the-query flags, in both CLI and tool spelling
    for flag in ("--limit", "--type", "--agent"):
        assert flag in body, flag
    for kw in ("`limit`", "`type`", "`agent_id`"):
        assert kw in body, kw
    # scan-then-zoom: raw tier is opt-in, in both spellings
    assert "--full" in body
    assert "full=True" in body


def test_introspect_search_entry_spans_all_zones_and_content() -> None:
    body = load_agent_skill(_skills_dir(), "imp-introspect")
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
