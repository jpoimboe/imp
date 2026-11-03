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


def test_introspection_places_the_snapshot_in_the_quest_home() -> None:
    flat = " ".join(_introspection_doc().split())
    assert "`~/.imp/projects/<slug>/quests/<id>/workflow/`" in flat
    assert "through the symlink `.imp/quests/<id>/workflow/`" in flat


def test_introspection_names_ground_truth_files() -> None:
    body = _introspection_doc()
    assert "workflow/workflow.yaml" in body
    assert "state/state.json" in body


def test_introspection_names_the_read_commands() -> None:
    body = _introspection_doc()
    assert "imp help" in body
    assert "imp list" in body
    assert "imp list --all" in body
    assert "imp list --json" in body
    assert "imp search" in body
    assert "imp show <id> spec" in body
    assert "imp events <id>" in body
    # its own bullet, not a passing mention: the guide says what the structured
    # board is and that it composes with the filters
    assert ("`imp list --json` — the same board as a structured JSON array, one "
            "object per quest tagged with its zone; composes with every filter."
            ) in " ".join(body.split())


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
    # Export carry-through and what --follow does / does not take are documented.
    assert "`--md`/`--html` exports" in body
    flat = " ".join(body.split())
    # The content flags reach an export too, and the guide says what they do there.
    assert ("as do the content flags `--full` / `--show` / `--hide`: an export "
            "starts from the same lean view the terminal shows, `--show <cat>` "
            "adds a category as a collapsed `<details>` fold, and `--full` "
            "expands every fold inline.") in flat
    assert ("`--follow` takes no windowing filters but composes with the "
            "content flags.") in flat
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
    flat = " ".join(body.split())
    # imp search and imp events search span active + backlog + history.
    for zone in ("**active**", "**backlog**", "**history**"):
        assert zone in body, zone
    assert "**archive**" not in body
    assert "conversation content" in flat.lower()   # the bullet wraps mid-phrase
    for flag in ("--active", "--backlog", "--archived", "--history", "-l"):
        assert flag in body, flag
    assert "`--archived` meaning archived conversations only" in flat
    # the broad --archived zone flag is gone from both search entries
    assert "`--active` / `--backlog` / `--archived` flags" not in flat
    assert "`--active` / `--backlog` / `--history` flags scope it" in flat
    assert "active → backlog → history" in flat
    assert "search the archive (completed, abandoned, and left" not in body


def test_introspection_reader_section_says_history_for_filed_quests() -> None:
    flat = " ".join(_introspection_doc().split())
    assert "a *different* quest — live or in history —" in flat
    assert "Resolves live and history quests alike." in flat
    assert "live or archived" not in flat
    assert "imp list --all` — also includes abandoned and archived quests" in flat


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
    assert "imp/config.yaml" in body
    assert "imp/memory/" in body
    assert "guidance" not in low
    assert "default-workflow" in body
    assert "test_cmd_fast" in body
    assert "test_cmd_slow" in body
    # the concrete config layering chain
    assert ("floor ← imp/config.yaml ← ~/.imp/config.yaml ← "
            "~/.imp/projects/<slug>/config.yaml") in body
    # how to read what is active, incl. imp config's blind spots
    assert "imp config" in body
    assert "blind spots" in body


def test_memory_doc_states_the_shared_three_state_shape() -> None:
    flat = " ".join(_memory_doc().split())
    assert ("`test_cmd_fast` / `test_cmd_slow` (and timeouts) — the commands the verify "
            "gates run. `test_cmd_slow` falls back to `test_cmd_fast` when it names no "
            "command of its own.") in flat
    assert ("`pythonpath` — worktree-relative import roots prepended to the gate/agent "
            "subprocess PYTHONPATH") in flat
    assert ("**unset** means never decided, and imp raises it once the repo can answer; "
            "**`false`** means this project needs none, and stays silent forever; "
            "anything else is the value.") in flat
    assert "empty string is a deliberate opt-out" not in flat


def test_memory_doc_states_the_settings_memory_root_split() -> None:
    body = _memory_doc()
    flat = " ".join(body.split())
    # the two CHECKOUT-KEYED settings layers are rooted in the main checkout, not the
    # worktree (~/.imp/config.yaml is machine-wide, so it is not claimed here)
    assert ("both checkout-keyed layers (`imp/config.yaml` and the local "
            "`~/.imp/projects/<slug>/config.yaml`) are read from the **main checkout** "
            "— never from a quest's worktree") in flat
    assert "a branch cannot change the `test_cmd_*`, `pythonpath`, or gate" in flat
    # imp/memory/ is the one deliberate exception, named on the tier bullet too
    assert "it is read from the quest's own worktree" in flat
    assert ("`imp/config.yaml` in the main checkout, `imp/memory/` in the quest's "
            "own worktree") in flat
    # the deleted facts channel leaves no trace
    assert "facts" not in flat.lower()
    assert "{{ project." not in flat


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


def test_knowledge_list_bullet_names_done_completions() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    # imp list now surfaces done (no-merge) completions alongside merged ones
    assert "completed quests (merged or done)" in body
    # the stale merged-only framing of the imp list bullet is gone
    assert "the backlog, and merged\n  quests;" not in body
    # the structured encoding rides the imp list bullet, not a neighbour's
    assert (body.index("completed quests (merged or done)")
            < body.index("`--json` emits the board as a structured array.")
            < body.index("- `imp show <id>`"))


def test_knowledge_cheatsheet_documents_bounded_events_search() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    flat = " ".join(body.split())
    assert "`imp events search <query>`" in body
    assert "structured-event search is bounded by default" in flat
    assert "`--type` / `--agent` narrow it." in body[
        body.index("- `imp events search <query>") : body.index("- `imp edit <id>")
    ]
    assert "`--list` to scan every matched quest" in flat
    assert "`imp events <id> ... --json` for exact payloads" in flat
    assert "`--all` only for exhaustive search output" in flat
    assert "backlog, and history quests' conversation content." in body
    assert "`--all` adds abandoned and archived quests" in flat


def test_knowledge_imp_edit_bullet_documents_base_flag() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    # the source wraps, so assert each within-line half separately
    assert "`--base <branch>` re-points the fork at another existing" in body
    assert "branch (also before go-live only)." in body
    # the clause rides the imp edit bullet: after --depends-on, before the next bullet
    assert (body.index("rejected once materialized).")
            < body.index("`--base <branch>` re-points")
            < body.index("- `imp help`"))


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


def test_memory_doc_documents_the_config_writer() -> None:
    body = _memory_doc()
    flat = " ".join(body.split())
    assert "there is no `imp config set`" not in flat
    assert "`imp config set <key> <value>`" in flat
    assert "`imp config unset <key>`" in flat


def test_knowledge_cheat_sheet_lists_the_config_writer() -> None:
    body = load_internal_skill(_skills_dir(), "imp-knowledge")
    flat = " ".join(body.split())
    assert "`imp config set <key> <value>` / `unset <key>`" in flat
    # the writer targets the MAIN CHECKOUT's project layer, never a worktree's
    assert "the setting's own layer (the main checkout's `imp/config.yaml` or" in flat


def test_memory_doc_names_the_local_write_scope() -> None:
    flat = " ".join(_memory_doc().split())
    assert "`--project` / `--global` / `--local`" in flat
    assert ("`--local` writes this checkout's `~/.imp/projects/<slug>/config.yaml`, "
            "which tops the stack and is never committed") in flat
    # `imp config` leads with the resolved state directory
    assert "Its first line, `project state:`, names this checkout's state directory." in flat


def test_memory_doc_documents_worktree_setup() -> None:
    flat = " ".join(_memory_doc().split())
    assert (
        "`worktree_setup` — a shell command imp runs in each newly created quest "
        "worktree, before the first step, for toolchains that keep dependencies in a "
        "gitignored per-tree directory (`.venv`, `node_modules`, `vendor/bundle`) that "
        "`git worktree` does not materialize."
    ) in flat


def test_knowledge_cheat_sheet_names_the_local_write_scope() -> None:
    flat = " ".join(load_internal_skill(_skills_dir(), "imp-knowledge").split())
    assert "led by this checkout's `project state:` directory (not under `--explain`)" in flat
    assert "`--project` / `--global` / `--local` forces one" in flat
    assert "`~/.imp/projects/<name>/config.yaml`" in flat


def test_knowledge_names_worktree_setup_among_the_settings_init_confirms() -> None:
    flat = " ".join(load_internal_skill(_skills_dir(), "imp-knowledge").split())
    assert ("detects the default workflow, integration branch, fast test command, "
            "`worktree_setup`, and import root and confirms them on one screen") in flat


def test_knowledge_cheatsheet_distinguishes_log_narrative_from_events_digest() -> None:
    flat = " ".join(load_internal_skill(_skills_dir(), "imp-knowledge").split())
    assert ('- `imp log <id>` — the human "read the story" narrative, tool bursts '
            "folded; `--step <name>` scopes it to one step, `-s` swaps the story "
            "for the per-step ledger `imp show` also carries, and owns its "
            "windowing and `--md`/`--html` export.") in flat
    assert ("- `imp events <id>` — the agent-facing digest, where event content "
            "actually lives: bare prints an index (steps, subagents, types); add a "
            "leaf selector (`--limit N`, `--type`, `--agent <handle>`, a range, "
            "`--all`) for one compact line per event, `--step <name>` to scope, "
            "`--json` for exact payloads.") in flat
    # the wording that steered agents at the folded human view is gone
    assert "the readable narrative of what happened" not in flat
    assert "the event index (steps, subagents, types)" not in flat
    # the digest bullet still leads the events-search bullet that refines it
    assert (flat.index("the agent-facing digest")
            < flat.index("imp events search <query>"))


def test_introspection_documents_the_summary_ledger_and_its_quota_column() -> None:
    # AC 20: the column and the two reaches it does not cover are recorded.
    flat = " ".join(_introspection_doc().split())
    assert "## The summary ledger" in _introspection_doc()
    assert "`imp log <id> -s` reads a run as a ledger rather than a story" in flat
    assert ("the share of the account's weekly quota window this quest"
            " consumed, attributed to *this* quest rather than reported as raw"
            " gauge movement") in flat
    # limit 1: a project pinned outside the discovered tree is absorbed
    assert "hand-pinned with `$IMP_STATE_DIR` is not discoverable" in flat
    # limit 2: the gauge's own quantization
    assert "carries ±1 point of quantization" in flat


def test_knowledge_and_introspection_name_the_verify_artifact_view() -> None:
    knowledge = load_internal_skill(_skills_dir(), "imp-knowledge")
    assert "show <id> verify" in knowledge
    intro = " ".join(_introspection_doc().split())
    assert "`imp show <id> spec` for the raw spec" in intro
    assert "`imp show <id> verify` for the last failing test-gate run" in intro
    # the cross-quest reader list enumerates the facets a second time
    assert "Add `spec` for the raw spec, `verify` for the last failing gate run" in intro
