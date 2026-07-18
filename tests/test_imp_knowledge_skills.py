"""imp-knowledge is a flat, self-contained quick reference: inline CLI cheat-sheet,
memory layering, and glossary, with a {{ docs.path }} corpus pointer for depth.
Introspection/memory depth lives in docs/guide/introspection.md and
docs/guide/memory.md, covered here by reading those files directly."""

from __future__ import annotations

from pathlib import Path

import pytest

from imp.agents.skills import SkillNotFound, load_internal_skill
from imp.assets import bundled_skills_dir


def _introspection_doc() -> str:
    return Path("docs/guide/introspection.md").read_text()


def _memory_doc() -> str:
    return Path("docs/guide/memory.md").read_text()


def _skills_dir():
    return bundled_skills_dir()


def test_knowledge_skill_is_loadable_profile_b() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    assert body.strip(), "imp-knowledge is empty"
    assert not body.lstrip().startswith("---"), "imp-knowledge has frontmatter"


def test_knowledge_maps_into_the_corpus_by_path_variable() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    # the injected corpus-path variable is retained as the depth pointer
    assert "{{ docs.path }}" in body


def test_knowledge_hub_carries_no_inlined_lifecycle_depth() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    # lifecycle depth moved to docs/guide/lifecycle.md — the hub only points at it
    for moved in ("discard_conversation", "save_as_draft", "archive_conversation",
                  "create_and_enter_worktree"):
        assert moved not in body, moved


def test_knowledge_vocabulary_uses_tier_not_lane() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    low = body.lower()
    for term in ("spec", "plan", "phase", "step", "tier", "workflow"):
        assert term in low, term
    # the incorrect `lane` vocabulary entry is gone; the correct term is `tier`
    assert "**lane**" not in body
    assert "a model tier a step runs at" not in body
    assert "**tier**" in body


def test_knowledge_hub_no_longer_maps_to_removed_spokes() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    assert 'load_skill(name="imp-introspection")' not in body
    assert 'load_skill(name="imp-memory")' not in body


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


def test_introspection_bare_events_call_names_the_id() -> None:
    body = _introspection_doc()
    # "bare call" means omitting the leaf selectors, not the id — the id is
    # always required. Both digest-tier sentences name `imp events <id>` so the
    # reader cannot misread "bare" as "id-less" (a most-recent default).
    assert "A bare `imp events <id>` call prints a self-describing index" in body
    assert "A bare `imp events <id>` call prints the index" in body
    # No most-recent id-less default is claimed anywhere in the doc.
    assert "most recent quest" not in body.lower()


def test_introspection_documents_events_full_and_one_row() -> None:
    body = _introspection_doc()
    low = body.lower()
    # The digest tier documents the one-terminal-row clip and the --full widen.
    assert "one terminal row" in low
    assert "--full" in body
    # The stale "--json is byte-capped" claim is gone from the raw tier.
    assert "byte-capped" not in low


def test_introspection_documents_log_windowing_filters() -> None:
    body = _introspection_doc()
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


def test_memory_doc_is_present_and_frontmatter_free() -> None:
    body = _memory_doc()
    assert body.strip(), "memory doc is empty"
    assert not body.lstrip().startswith("---"), "memory doc has frontmatter"


def test_memory_covers_two_tiers_of_durable_state() -> None:
    body = _memory_doc()
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


def test_old_skill_names_are_gone() -> None:
    for old in ("imp-mental-model", "imp-introspect"):
        with pytest.raises(SkillNotFound):
            load_internal_skill(_skills_dir(), old)


def test_memory_teaches_the_in_depth_model() -> None:
    body = _memory_doc()
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
    body = _memory_doc()
    assert "## Building up memory with imp" in body
    # The affordance names the interactive gates and both routes (standalone
    # background quest / coupled edit gated by validate-memory), and rewords the
    # invariant as consent + path.
    assert "background `memory` quest" in body
    assert "only with the user's explicit agreement and only ever under `imp/memory/`" in body
    assert "A preference **coupled**" in body
    assert "`validate-memory` gate" in body
    assert "atomically" in body
    assert "threshold" in body
    assert "finalize" in body


def test_knowledge_cheatsheet_documents_events_search() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    # the new cheat-sheet bullet for the structured-event search command
    assert "imp events search <query>" in body
    assert "content (tool calls, results, reasoning) across all quests, reaching what" in body
    assert "(prose only) can't; `--type` / `--agent` narrow it." in body
    # the imp search bullet now names conversation content as its match target
    assert "backlog, and archived quests' conversation content." in body


def test_knowledge_memory_line_uses_background_and_carries_imp_edit_bullet() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    # the memory-recording line now launches the memory quest in the background
    assert 'imp new --workflow memory --background "<what to change>"' in body
    assert "coupled to a code quest rides that quest's spec as a Memory changes item" in body
    assert 'imp new --workflow memory "<what to change>"' not in body
    # the new imp edit cheat-sheet bullet, verbatim in its load-bearing parts
    assert "- `imp edit <id>` — link or annotate a quest that hasn't run yet:" in body
    # the inline code span wraps across a newline in the source, so assert each
    # within-line half separately (load_internal_skill returns raw, unnormalized text)
    assert "--append-intent" in body
    assert '"[<quest>] <note>"' in body
    assert "cross-links a note onto another quest's opening intent" in body
    assert "`--depends-on <id>` adds a dependency edge (only before go-live" in body
    assert "rejected once materialized)." in body
    # placement: after the events-search bullet, before the imp help bullet
    assert (body.index("imp events search <query>")
            < body.index("imp edit <id>")
            < body.index("the command list, or one command"))
