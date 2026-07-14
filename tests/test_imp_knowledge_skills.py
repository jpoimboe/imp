"""The grounding skills (imp-knowledge, imp-introspection, imp-memory) ship in the
default workflow, are loadable via the skills mechanism with no frontmatter
(Profile B), and cover the spec-mandated topics verified against live code."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.skills import SkillNotFound, load_internal_skill
from imp.assets import bundled_skills_dir


def _introspection_doc() -> str:
    return Path("docs/guide/introspection.md").read_text()


def _skills_dir():
    return bundled_skills_dir()


def test_knowledge_skill_is_loadable_profile_b() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    assert body.strip(), "imp-knowledge is empty"
    assert not body.lstrip().startswith("---"), "imp-knowledge has frontmatter"


def test_knowledge_lists_status_set_and_draft_relabel() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge").lower()
    for status in ("new", "running", "paused", "saved", "completed",
                   "merge_blocked", "abandoned"):
        assert status in body, status
    assert "errored" not in body
    assert "draft" in body


def test_knowledge_names_the_three_left_draft_dispositions() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    assert "discard_conversation" in body
    assert "save_as_draft" in body
    assert "archive_conversation" in body


def test_knowledge_covers_home_zones_materialize_and_vocabulary() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge").lower()
    assert ".imp/quests/" in body
    assert "backlog" in body
    assert "materialize" in body
    for term in ("spec", "plan", "phase", "step", "lane"):
        assert term in body, term


def test_introspection_skill_is_loadable_profile_b() -> None:
    body = _introspection_doc()
    assert body.strip(), "imp-introspection is empty"
    assert not body.lstrip().startswith("---"), "imp-introspection has frontmatter"


def test_introspection_names_ground_truth_files() -> None:
    body = _introspection_doc()
    assert "workflow/workflow.yaml" in body
    assert "state/state.json" in body


def test_introspection_names_the_read_commands() -> None:
    body = _introspection_doc()
    assert "imp help" in body
    assert "imp list" in body
    assert "imp list --all" in body
    assert "imp search" in body
    assert "imp show <id> spec" in body
    assert "imp events <id>" in body


def test_introspection_documents_the_three_tier_split() -> None:
    body = _introspection_doc().lower()
    # narrative / digest / raw are the three named timeline tiers
    for tier in ("narrative", "digest", "raw"):
        assert tier in body, tier
    # narrative tier names imp log; digest tier names imp events
    assert "imp log <id>" in body
    assert "imp events <id>" in body


def test_introspection_documents_events_full_and_one_row() -> None:
    body = load_internal_skill(_skills_dir(), "imp-introspection")
    low = body.lower()
    # The digest tier documents the one-terminal-row clip and the --full widen.
    assert "one terminal row" in low
    assert "--full" in body
    # The stale "--json is byte-capped" claim is gone from the raw tier.
    assert "byte-capped" not in low


def test_introspection_documents_log_windowing_filters() -> None:
    body = load_internal_skill(_skills_dir(), "imp-introspection")
    # imp log now takes the same windowing filters imp events has (no --type).
    assert "imp log <id> --step" in body
    # The full windowing set is documented together in the log context, tying
    # --limit / --agent to imp log rather than matching their pre-existing
    # occurrences elsewhere in the skill.
    assert "`--step`, `--range` (or a positional `<range>`), `--limit`, and `--agent`" in body
    # The --type exclusion is explicitly called out with its rationale.
    assert "fights the folded narrative" in body
    # Export carry-through and the --follow exemption are documented.
    assert "`--md`/`--html` exports" in body
    assert "`--follow` stays unfiltered" in body
    # The named-consumer read of the seed conversation is called out.
    assert "--step threshold" in body


def test_introspection_documents_the_cross_change_query() -> None:
    body = _introspection_doc()
    # CLI front doors for cross-quest reads
    assert "imp show <id>" in body
    assert "imp events <id>" in body
    assert "cat-ing its files" in body


def test_introspection_documents_bounded_output_techniques() -> None:
    body = _introspection_doc()
    # scope-the-query flags (CLI)
    for flag in ("--limit", "--type", "--agent"):
        assert flag in body, flag
    # scan-then-zoom: the raw tier is opt-in via --json
    assert "--json" in body


def test_introspection_search_entry_spans_all_zones_and_content() -> None:
    body = _introspection_doc()
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


def test_introspection_documents_merged_search_and_merge_commit() -> None:
    body = _introspection_doc()
    # The merged-subset flag is documented alongside the zone flags.
    assert "--merged" in body
    # The imp show detail card surfaces the quest's merge commit.
    assert "merge_commit" in body
    # The stale archive-only framing must not be reintroduced by the edit.
    assert "search the archive (completed, abandoned, and left" not in body


def test_memory_skill_is_loadable_profile_b() -> None:
    body = load_internal_skill(_skills_dir(), "imp-memory")
    assert body.strip(), "imp-memory is empty"
    assert not body.lstrip().startswith("---"), "imp-memory has frontmatter"


def test_memory_covers_two_tiers_of_durable_state() -> None:
    body = load_internal_skill(_skills_dir(), "imp-memory")
    low = body.lower()
    # two-tiers framing
    assert "two tiers" in low
    # per-quest records tier
    assert ".imp/quests/" in body
    # project-config tier
    assert "imp/project.yaml" in body
    assert "imp/memory/" in body
    assert "guidance" not in low
    assert "facts" in low
    assert "default-workflow" in body
    assert "test_cmd_fast" in body
    assert "test_cmd_slow" in body
    # the concrete config layering chain
    assert "floor ← imp/project.yaml ← ~/.config/imp/config.yaml ← .imp/config.yaml" in body
    # how to read what is active, incl. imp config's blind spots
    assert "imp config" in body
    assert "blind spots" in body


def test_knowledge_no_longer_references_removed_guidance_key() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    assert "guidance" not in body.lower()
    assert "imp/memory/" in body


def test_knowledge_maps_to_both_spokes() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    assert 'load_skill(name="imp-introspection")' in body
    assert 'load_skill(name="imp-memory")' in body


def test_old_skill_names_are_gone() -> None:
    for old in ("imp-mental-model", "imp-introspect"):
        with pytest.raises(SkillNotFound):
            load_internal_skill(_skills_dir(), old)


def test_memory_teaches_the_in_depth_model() -> None:
    body = load_internal_skill(_skills_dir(), "imp-memory")
    low = body.lower()
    # The new in-depth section and its additive-layering claim.
    assert "## The memory model in depth" in body
    assert "additive" in low
    assert "all.md`, then `W/all.md`, then `W/<step>.md" in body
    # Step-name discovery from workflow.yaml, load-time validation, and the freeze.
    assert "workflow.yaml" in body
    assert "validates the whole `imp/memory/` tree at load" in body
    # "upgrade-orphan" and "detection" wrap across a line break in the pinned
    # prose, so assert the distinctive token alone (raw file body, no whitespace
    # normalization).
    assert "upgrade-orphan" in low
    assert "freezes" in low
    # No stale guidance reference reintroduced by the enrichment.
    assert "guidance" not in low


def test_memory_teaches_the_authoring_affordance() -> None:
    body = load_internal_skill(_skills_dir(), "imp-memory")
    assert "## Building up memory with imp" in body
    # The affordance names the interactive gates and the background memory
    # quest, and states imp never auto-writes memory.
    assert "background `memory` quest" in body
    assert "never auto-writes memory" in body
    assert "threshold" in body
    assert "finalize" in body


def test_knowledge_memory_entry_notes_authoring_help() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    # The refreshed map entry points past inspection to authoring help,
    # while keeping the load_skill invocation intact.
    assert 'load_skill(name="imp-memory")' in body
    assert "help you author memory" in body


def test_knowledge_archive_zone_says_shelved_not_left() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    # The archive-zone disposition noun is "shelved", never "left".
    assert "shelved conversations" in body
    assert "left conversations" not in body
    # The archive_conversation outcome carries the shelved cue.
    assert "resumable (shelved)" in body


def test_knowledge_backlog_zone_names_backlog_origin_only() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    # The backlog zone is described by the single surviving origin word.
    assert "backlog items" in body
    assert "`origin` `backlog`" in body
    # The collapsed origin tokens are gone as origin values (backtick-quoted);
    # ordinary prose like "from idea to merge" is untouched.
    assert "`idea`" not in body
    assert "`followup`" not in body
