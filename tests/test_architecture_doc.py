"""docs/internals/architecture.md and its sibling guides are living docs: they name the
renamed artifact ref, and their `## Default workflow` narrative, diagrams, and reference
tables stay reconciled with the shipped default workflow."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ARCH = Path("docs/internals/architecture.md")
DEFAULT_WORKFLOW = Path("src/imp/assets/workflows/default/workflow.yaml")

# `implementer` is omitted deliberately: it is the live minimal-workflow agent.
RETIRED_AGENT_NAMES = (
    "spec-writer",
    "integrator",
    "integrate-verify",
    "review-panel",
    "plan-cycle-check",
    "commit-check",
    "stamp-trailers",
    "base-fresh-check",
    "completeness-slice",
    "spec-compliance-slice",
    "boundaries-slice",
    "quality-reviewer",
)


def test_architecture_uses_renamed_artifact_ref() -> None:
    text = ARCH.read_text()
    assert "journal-wip" not in text
    assert "refs/imp/artifacts/<id>" in text


def test_architecture_drops_artifact_commit_command() -> None:
    assert "artifact-commit" not in ARCH.read_text()


def test_architecture_names_quest_concept() -> None:
    text = ARCH.read_text()
    assert "| Quest / state | `engine/quest.py`, `engine/state.py` |" in text
    assert "A single **quest** is one unit of work" in text
    assert "the last plan of a multi-plan quest" in text
    assert "engine/change.py" not in text
    assert "| Change / state |" not in text
    assert ".imp/change/" not in text
    assert "multi-plan change" not in text


def test_what_is_imp_names_quest_concept() -> None:
    text = Path("docs/guide/what-is-imp.md").read_text()
    assert "The core idea is a **quest**" in text
    assert "per-quest home on disk." in text
    assert "**Change**" not in text


def test_imp_conventions_names_quest_concept() -> None:
    text = Path("docs/internals/conventions.md").read_text()
    assert "nouns: quest, workflow, backlog, phase," in text
    assert "nouns: Change," not in text


def test_imp_invariants_names_quest_concept() -> None:
    text = Path("docs/internals/invariants.md").read_text()
    assert "quest.json" in text
    assert "change.json" not in text
    assert "change_id" not in text
    assert ".imp/changes/" not in text


def test_imp_operator_notes_names_quest_concept() -> None:
    text = Path("docs/guide/operator-notes.md").read_text()
    assert "Runbook for operating a running imp quest." in text
    assert ".imp/quests/<id>/workflow/" in text
    assert ".imp/quest/workflow" not in text
    assert ".imp/change/" not in text
    assert ".imp/changes/" not in text


def test_architecture_documents_the_minimal_workflow() -> None:
    text = ARCH.read_text()
    low = text.lower()
    assert "## Minimal workflow" in text
    assert "src/imp/assets/workflows/minimal/" in text
    # forks a worktree on imp/<id>, no plan, no merge
    assert "no merge" in low
    # the three steps
    for step in ("front-door", "implement", "review"):
        assert step in text
    minimal_section = text.split("## Minimal workflow", 1)[1]
    low_section = minimal_section.lower()
    # the read-only reviewer diffs the fork point; the branch is kept on completion
    assert "worktree" in low_section
    assert "read-only" in low_section
    assert "cur_base_head" in minimal_section
    assert "imp/<id>" in minimal_section
    # stale in-place / capture-base / base_head claims are gone
    assert "capture-base" not in text
    assert "no worktree is forked" not in text
    assert "in-place" not in low_section


def test_architecture_documents_the_done_terminal_status() -> None:
    text = ARCH.read_text()
    assert "terminal status" in text
    assert "`done`" in text
    # a no-merge completion is archived, not merged
    assert "archived" in text.lower()


def test_architecture_documents_the_shared_partials_overlay() -> None:
    text = ARCH.read_text()
    low = text.lower()
    assert "assets/partials/" in text
    assert "overlaid" in low
    assert "local-first" in low


def test_architecture_notes_the_workflow_memory_rename() -> None:
    text = ARCH.read_text()
    assert "imp/memory/default/" in text
    assert "imp/memory/imp/" in text


def test_architecture_names_no_bundled_setup_agent() -> None:
    text = ARCH.read_text()
    # `imp init` is deterministic: there is no bundled agent outside a workflow.
    assert "assets/init.md" not in text
    assert "setup agent" not in text


def test_architecture_documents_the_out_of_checkout_state_directory() -> None:
    text = ARCH.read_text()
    assert "### Where state lives" in text
    assert "`~/.imp/projects/<slug>/`" in text
    assert "project_state_dir()" in text
    assert "$IMP_STATE_DIR" in text
    assert "$IMP_HOME" in text
    assert "`repo` marker" in text
    # AC 26(b): the backfill touches only imp-owned dirs, so this promise stays true.
    assert "overrides the per-project path verbatim — no slug walk, no marker written" in text
    for entry in ("`quests/<id>/`", "`worktrees/<id>/`", "`archive/`"):
        assert entry in text, entry
    # the one imp path left in a worktree is the symlink into the quest home
    assert "`.imp/quests/<id>` pointing at that quest's home" in text
    # no checkout-relative state path survives anywhere in the doc
    for stale in (".imp/quest/", ".imp/archive", ".imp/worktrees"):
        assert stale not in text, stale


def test_architecture_names_imp_home_as_engine_injected() -> None:
    # AC 11(b): IMP_HOME is engine-emitted, not inherited — a sandboxed agent gets it
    # from engine_env or not at all, so the doc must name it beside IMP_STATE_DIR.
    text = ARCH.read_text()
    assert ("`engine_env()` injects `$IMP_HOME` unconditionally and `$IMP_STATE_DIR` "
            "wherever it is given a control root — every policy bake — so both reach "
            "every agent subprocess") in text


def test_operator_notes_document_the_moved_checkout_limitation() -> None:
    text = Path("docs/guide/operator-notes.md").read_text()
    flat = " ".join(text.split())
    assert "## Moving or renaming a checkout" in text
    assert "`repo` marker" in text
    assert "orphaned" in flat
    # the manual recovery: re-slug the directory, repoint the marker, repair git
    assert "git worktree repair" in text
    # imp already claimed an empty <new-name>; an unguarded mv nests the
    # orphaned dir inside that stub, leaving `imp config` correct over wrong contents
    assert '[ -e "$NEW" ]' in text
    assert 'mv "$OLD" "$NEW"' in text
    assert '[ "$(ls -A "$NEW" 2>/dev/null)" = repo ] && rm -rf "$NEW"' in text
    assert "imp list" in text
    # in-flight quests record absolute paths that the rename does not rewrite
    assert "Finish or abandon in-flight quests before moving a checkout" in text


def test_operator_notes_document_dead_provider_login_recovery() -> None:
    text = Path("docs/guide/operator-notes.md").read_text()
    assert "## Recovering a dead provider login" in text
    # Placed between the pause runbook and the checkout-move section.
    assert (text.index("## Unblocking a paused quest")
            < text.index("## Recovering a dead provider login")
            < text.index("## Moving or renaming a checkout"))
    section = " ".join(
        text.split("## Recovering a dead provider login", 1)[1]
            .split("## Moving or renaming a checkout", 1)[0].split())
    # single-use rotation, refreshed by imp itself, written back to both canonical files
    assert "single-use" in section
    assert "~/.codex/auth.json" in section
    assert "~/.claude/.credentials.json" in section
    # never copy a credential file between machines
    assert "Never copy a credential file between machines" in section
    assert "Log in separately on each host" in section
    # the pause and its remedy
    assert "reauth_needed" in section
    assert "`codex login`" in section and "`claude login`" in section
    assert "imp resume <id>" in section
    # copying a still-live shared file back, and the codex-only API-key fallthrough
    assert "another host still holds a live copy" in section
    assert "OPENAI_API_KEY" in section
    assert "bills the OpenAI API, not your ChatGPT subscription" in section


def test_operator_notes_document_archive_version_skew() -> None:
    from imp.engine.archive import _DATA_VERSION
    from imp.engine.errors import ArchiveMismatch

    text = Path("docs/guide/operator-notes.md").read_text()
    flat = " ".join(text.split())
    assert "## Archive version skew" in text
    section = " ".join(text.split("## Archive version skew", 1)[1].split())
    # quote is verbatim; update doc example numbers when _DATA_VERSION is bumped
    assert str(ArchiveMismatch(floor="data", stored=_DATA_VERSION + 1, known=_DATA_VERSION)) in flat
    assert "archive index is at v" in section
    # the remedy for a running quest, and the rule future schema work must follow
    assert "imp resume <id>" in section
    assert "a new index column must be nullable" in section
    assert "index.ingest" in section
    # the rule that keeps the split floor checks safe (mirrored at _DATA_VERSION)
    assert ("a blob migration that changes an index-visible value must also bump "
            "the index schema version") in section


def test_operator_notes_skew_names_the_stale_process_first() -> None:
    """A running imp is frozen at its launch-time code, so the usual skew is a
    long-lived process, not a behind checkout."""
    text = Path("docs/guide/operator-notes.md").read_text()
    section = " ".join(text.split("## Archive version skew", 1)[1].split())
    assert "The usual cause is a stale process, not a stale checkout" in section
    assert "warms its module graph at startup" in section
    assert "this checkout is behind" not in section


def test_operator_notes_skew_scope_excludes_a_live_quest() -> None:
    """Ids resolve against live homes and a live quest's data sits in its own
    home, so the refusal no longer reaches an id-resolving command for one."""
    text = Path("docs/guide/operator-notes.md").read_text()
    section = " ".join(text.split("## Archive version skew", 1)[1].split())
    assert "refuses only the reads that touch the floor that moved" in section
    assert "A live quest stays reachable either way" in section
    assert "on every command that touches the archive" not in section
    # each floor refuses whole commands: no read degrades part of one
    assert "refuses whole commands rather than degrading part of" in section
    assert "journal section" not in section


def test_operator_notes_document_the_unguarded_pinned_state_dir() -> None:
    text = Path("docs/guide/operator-notes.md").read_text()
    flat = " ".join(text.split())
    # A pinned dir is deliberately never marked by imp, so the ownership guard is off
    # until the operator writes the marker — the page has to say so and show the form.
    assert "A pinned directory carries no `repo` marker" in flat
    assert "Opt in to the ownership guard by writing the marker yourself" in flat
    assert 'pwd -P > "$DIR/repo"' in text


OVERVIEW_DIAGRAM = """flowchart LR
  start([intent]) --> TH["threshold"]
  TH --> SPEC["spec"]
  SPEC --> PLAN["plan"]
  PLAN --> IMPL["implement"]
  IMPL --> CONV["converge"]
  CONV --> INTG["integrate"]
  INTG --> done([merged])
  TH -. "express" .-> PLAN
  IMPL -. "next outline plan" .-> PLAN
  CONV -. "coverage gap" .-> PLAN
  CONV -. "task gap" .-> IMPL
  INTG -. "rework" .-> PLAN"""


def test_architecture_default_workflow_intro_is_the_six_phase_text() -> None:
    text = ARCH.read_text()
    assert "bundled with the install — a committed `imp/workflows/default` overrides it" in text
    assert (
        "Six phases. `threshold` is the front door — one conversation that answers "
        "questions, scouts existing work, or scopes a build." in text
    )
    assert (
        "The spec's `## Plan outline` seeds a plan count, and `plan` → `implement` then "
        "runs once per outline plan, carrying **no** code review and **no** deferred "
        "sweep in the per-plan loop." in text
    )
    assert "After the last plan of a multi-plan quest, `converge` runs **once**" in text
    assert (
        "An **express** quest (chosen at threshold) skips the whole spec phase and the "
        "`plan-reviewer` gate; everything from `implement` onward is identical." in text
    )
    # the five-phase model and the false "fast path skips the supercycle" claim are gone
    assert "Five phases" not in text
    assert "skips the spec and plan-document review gates and the supercycle" not in text


def test_architecture_overview_diagram_matches_the_shipped_phase_order() -> None:
    assert OVERVIEW_DIAGRAM in ARCH.read_text()


def test_architecture_legend_names_the_real_branch_steps() -> None:
    assert (
        "- **branch** (`{ }` diamonds) — a var router, no agent or script: "
        "`check-next-plan` and `wait-for-head` "
        "(which also blocks to acquire the merge lane)." in ARCH.read_text()
    )


THRESHOLD_DIAGRAM = """flowchart TD
  in([intent / seed]) --> TH["threshold<br/>(explorer · researcher · spiker)"]
  TH --> SCC{{"scope-code-check"}}
  SCC -->|express| toplan([→ plan-writer])
  SCC -->|full| tospec([→ spec-finalizer])
  SCC -. "code committed while scoping" .-> TH"""

SPEC_DIAGRAM = """flowchart TD
  in([from threshold]) --> SF["spec-finalizer<br/>(explorer · researcher · spiker)"]
  SF --> SVC{{"spec-verify-commit"}}
  SVC -. "no spec / no outline" .-> SF
  SVC -->|"plan_total = N"| SR["spec-reviewer"]
  SR -->|"Critical / Major"| SF
  SR -->|"minors: one pass"| SF
  SR -->|clean| out([→ plan-writer])"""

PLAN_DIAGRAM = """flowchart TD
  in([from spec · outline plan N]) --> PW["plan-writer<br/>(explorer · researcher)"]
  PW --> PVC{{"plan-verify-commit"}}
  PVC -. "no plan artifact" .-> PW
  PVC -->|express| PF
  PVC --> PR["plan-reviewer"]
  PR -->|"Critical / Major"| PW
  PR -->|"minors: one pass"| PW
  PR -->|clean| PF{{"plan-finalized"}}
  PF -->|"task_total = N"| out([→ task-lead])"""


def test_architecture_front_phase_sections_are_name_titled() -> None:
    text = ARCH.read_text()
    for heading in ("### Phase threshold", "### Phase spec", "### Phase plan"):
        assert heading in text, heading
    for stale in ("### Phase ① spec", "### Phase ② plan"):
        assert stale not in text, stale
    assert "spec-writer<br/>" not in text


def test_architecture_front_phase_diagrams_match_the_workflow() -> None:
    text = ARCH.read_text()
    for diagram in (THRESHOLD_DIAGRAM, SPEC_DIAGRAM, PLAN_DIAGRAM):
        assert diagram in text, diagram.splitlines()[1]


def test_architecture_front_phase_prose_names_key_steps() -> None:
    text = ARCH.read_text()
    for step in (
        "`scope-code-check`",
        "`spec-finalizer`",
        "`plan-writer`",
        "`plan-reviewer`",
        "`plan-finalized`",
    ):
        assert step in text, step


IMPLEMENT_DIAGRAM = """flowchart TD
  in([from plan]) --> TL["task-lead<br/>coder + code-reviewer"]
  TL -->|"verify tolerated"| CNP
  TL --> V{{"verify · fast"}}
  V -. fail .-> TL
  V -. timeout .-> HUMAN([pause → human])
  V --> CNP{"check-next-plan"}
  CNP -->|"more outline plans"| toplan([→ plan-writer])
  CNP -->|"coverage settled (rework)"| toqr([→ quest-review])
  CNP -->|"last outline plan"| out([→ gap-check])"""

CONVERGE_DIAGRAM = """flowchart TD
  in([from implement]) --> GC["gap-check"]
  GC -->|"gap → corrective plan (supercycle)"| toplan([→ plan-writer])
  GC -->|clean| QR["quest-review<br/>(batch-code-reviewer)"]
  QR -->|task_gap| totl([→ task-lead])
  QR -. Error .-> HUMAN([pause → human])
  QR --> DS["deferred-sweep<br/>coder + code-reviewer"]
  DS -->|"slow gate tolerated"| out([→ wait-for-head])
  DS --> CV{{"converge-verify · slow"}}
  CV -. "fail → triage" .-> DS
  CV -. timeout .-> HUMAN
  CV --> out"""

INTEGRATE_DIAGRAM = """flowchart TD
  in([from converge]) --> WFH{"wait-for-head<br/>(merge lane)"}
  WFH --> IR{{"integrate-rebase"}}
  IR -->|"base unmoved, tree cleared"| SUM
  IR -->|"rebased / needs re-verify"| VM
  IR -->|conflicts| REC["reconciler"]
  IR -. "bad ref / dirty tree" .-> HUMAN([pause → human])
  REC --> VM{{"validate-memory"}}
  REC -. stuck .-> HUMAN
  VM -. "invalid imp/memory/" .-> REC
  VM --> IV{{"integration-verify · slow"}}
  IV -. timeout .-> HUMAN
  IV --> IRV["integration-reviewer"]
  IRV -->|"critical/major / minor"| REC
  IRV -. Error .-> HUMAN
  IRV --> CC{{"check-cleared"}}
  CC -->|"not cleared"| IV
  CC -. error .-> HUMAN
  CC --> SUM["summarizer"]
  SUM --> FIN["finalize<br/>HUMAN: merge / rework / pause"]
  FIN -->|rework| back([→ plan-writer])
  FIN -. pause .-> HUMAN
  FIN -->|merge| CM{{"cleanup-merge"}}
  CM -->|"fixup conflict / fold changed the tree"| ASR["autosquash-resolve-merge"]
  ASR --> CM
  CM -. "unfoldable fixup / stamp failure" .-> HUMAN
  CM --> AST{{"assert-stamped"}}
  AST -. "unstamped / fixup left" .-> HUMAN
  AST --> M2B["merge-to-base"]
  M2B -->|MERGED| done([quest complete<br/>worktree removed · archived])
  M2B -->|CONFLICT| IR
  M2B -->|DIRTY| REC
  M2B -. REFUSED .-> HUMAN"""


def test_architecture_back_phase_sections_are_name_titled() -> None:
    text = ARCH.read_text()
    for heading in ("### Phase implement", "### Phase converge", "### Phase integrate"):
        assert heading in text, heading
    for stale in (
        "### Phase ③ implement",
        "### Phase ④ review",
        "### Phase ⑤ integrate",
    ):
        assert stale not in text, stale


def test_architecture_back_phase_diagrams_match_the_workflow() -> None:
    text = ARCH.read_text()
    for diagram in (IMPLEMENT_DIAGRAM, CONVERGE_DIAGRAM, INTEGRATE_DIAGRAM):
        assert diagram in text, diagram.splitlines()[1]


def test_architecture_implement_phase_disclaims_the_moved_work() -> None:
    # the phase prose is hard-wrapped; compare against a whitespace-flattened copy
    flat = " ".join(ARCH.read_text().split())
    assert (
        "There is no code review, no autosquash, and no deferred sweep in this phase — "
        "all of that moved to `converge`." in flat
    )
    assert "`verify` runs the project's fast test gate" in flat


def test_architecture_integrate_phase_prose_names_the_merge_door() -> None:
    # the phase prose is hard-wrapped; compare against a whitespace-flattened copy
    flat = " ".join(ARCH.read_text().split())
    assert (
        "`check-cleared` then re-asks the shared clearance predicate at the merge "
        "door: a tree the slow gate never passed — an environmental failure the "
        "reviewer repaired, say — routes back to `integration-verify` instead of "
        "reaching the human." in flat
    )


def test_architecture_names_no_retired_reviewer_verdict_field() -> None:
    text = ARCH.read_text()
    assert "blocking iff" not in text
    assert '"blocking / minors"' not in text
    assert "has_minor_findings" not in text


def test_architecture_step_agent_table_pairs_files_with_steps_and_tiers() -> None:
    text = ARCH.read_text()
    assert "| Agent file | Step | Tier | Role |" in text
    for row in (
        "| `threshold` | `threshold` | high |",
        "| `spec-finalizer` | `spec-finalizer` | high |",
        "| `spec-reviewer` | `spec-reviewer` | high |",
        "| `plan-writer` | `plan-writer` | high |",
        "| `plan-reviewer` | `plan-reviewer` | high |",
        # the three whose step name differs from the agent file name
        "| `batch-code-reviewer` | `quest-review` | high |",
        "| `integration-review` | `integration-reviewer` | high |",
        "| `autosquash-resolve` | `autosquash-resolve-merge` | high |",
        # workflow.yaml declares task-lead high; the old table claimed medium
        "| `task-lead` | `task-lead` | high |",
        "| `gap-check` | `gap-check` | high |",
        "| `deferred-sweep` | `deferred-sweep` | high |",
        "| `reconciler` | `reconciler` | high |",
        "| `finalize` | `finalize` | high |",
        # the one non-high step in the whole workflow
        "| `summarizer` | `summarizer` | medium |",
    ):
        assert row in text, row


def test_architecture_agent_table_names_both_autosquash_entry_modes() -> None:
    assert (
        "| `autosquash-resolve` | `autosquash-resolve-merge` | high | Make the fixup "
        "fold reproduce the pre-squash tree: resolve a conflict, or repair a fold "
        "that changed content. |"
    ) in ARCH.read_text()


def test_architecture_subagent_table_is_separate_and_names_dispatchers() -> None:
    text = ARCH.read_text()
    flat = " ".join(text.split())
    assert "dispatched via `subagents:` frontmatter" in flat
    assert "**never** a workflow step" in flat
    assert "| Subagent | Dispatched by |" in text
    for row in (
        "| `coder` | `task-lead`, `deferred-sweep` |",
        "| `code-reviewer` | `task-lead`, `deferred-sweep` |",
        "| `explorer` | `threshold`, `spec-finalizer`, `plan-writer`, `task-lead`, `finalize` |",
        "| `researcher` | `threshold`, `spec-finalizer`, `plan-writer`, `task-lead`, `finalize` |",
        "| `spiker` | `threshold`, `spec-finalizer`, `task-lead`, `finalize` |",
    ):
        assert row in text, row


def test_architecture_agent_reference_drops_the_deleted_agents() -> None:
    text = ARCH.read_text()
    for retired in (
        "`spec-writer`",
        "`review-panel`",
        "`quality-reviewer`",
        "`completeness-slice`",
        "`spec-compliance-slice`",
        "`boundaries-slice`",
        "`plan-cycle-check`",
        "`integrator`",
    ):
        assert retired not in text, retired
    # the summary artifact is read by `imp show`; `imp log --summary` is a
    # step-timing table built from events, not the artifact
    assert "queryable via `imp log`" not in text


def test_what_is_imp_lists_converge_between_implement_and_integrate() -> None:
    text = Path("docs/guide/what-is-imp.md").read_text()
    assert "- **converge** — reviewing and testing everything built, once." in text
    # user-facing copy uses only imp-canon nouns (docs/internals/conventions.md)
    # canon noun is "quest"; ban the noun sense only — "changes"/"unchanged" are fine
    assert not re.search(r"(?i)\b(?:a|the|this|each|per|your)\s+changes?\b", text)
    order = [
        text.index(f"- **{phase}**")
        for phase in ("threshold", "spec", "plan", "implement", "converge", "integrate")
    ]
    assert order == sorted(order), order


def test_operator_notes_name_the_spec_finalizer() -> None:
    text = Path("docs/guide/operator-notes.md").read_text()
    assert "`plan-writer` / `spec-finalizer`" in text
    assert "spec-writer" not in text


def test_invariants_exempt_the_reconciler_not_the_deleted_integrator() -> None:
    text = Path("docs/internals/invariants.md").read_text()
    flat = " ".join(text.split())
    assert "`reconciler` / `integration-review` reading `quest.json`" in flat
    assert "integrator" not in text


def test_invariants_forbid_handing_an_agent_a_masked_path() -> None:
    text = Path("docs/internals/invariants.md").read_text()
    flat = " ".join(text.split())
    assert ("- **Never hand an agent a path its sandbox masks.** "
            "`<quest_dir>/steps/` (the overlay-home mountpoints and transcripts) "
            "and `<quest_dir>/events.jsonl` are masked for every agent by "
            "`resolve_policy`. Engine-authored text that points a sandboxed agent "
            "at recovery data must inline the content or route through "
            "`imp events`, which the daemon resolves outside the sandbox.") in flat
    # it is a live layering invariant, not a future/unbuilt aspiration
    layering, _, _future = flat.partition("## Future / unbuilt")
    assert "Never hand an agent a path its sandbox masks." in layering


def test_invariants_pin_the_policy_to_the_quests_project_root() -> None:
    text = Path("docs/internals/invariants.md").read_text()
    flat = " ".join(text.split())
    assert ("- **The sandbox policy handed to an invocation must agree with "
            "`quest.project_root`.** Going live mid-step flips `project_root` "
            "to the new worktree; `_run_agent_invocation` reconciles the "
            "policy's `worktree` and `git_worktree_self` on every entry, so a "
            "retry or reincarnation cannot re-enter the step with a policy "
            "still rooted at the control root.") in flat
    # it is a live layering invariant, not a future/unbuilt aspiration
    layering, _, _future = flat.partition("## Future / unbuilt")
    assert "must agree with `quest.project_root`" in layering


def test_invariants_pin_codex_durability_and_bounded_interruption() -> None:
    text = Path("docs/internals/invariants.md").read_text()
    flat = " ".join(text.split())
    expected = (
        "- **Codex native session.** One run-scoped process owner linearizes "
        "cancellation with subprocess publication and identity-safe cleanup; a "
        "live hard stop captures the process tree before waking blocked driver "
        "I/O, and the wake also releases drains held after parent exit. The "
        "coordinator's two-second hard deadline is acknowledged by receive or "
        "teardown independently of tool cleanup. Normal finalization still "
        "persists the exact native thread and rollout before removing the "
        "temporary Codex home.")
    layering, _, _future = flat.partition("## Future / unbuilt")
    assert expected in layering


def test_architecture_engine_half_names_the_current_default_subagents() -> None:
    text = ARCH.read_text()
    assert (
        "Tools can spawn subagents (coder, code-reviewer), which re-enter the same "
        "drive loop one level down." in text
    )
    assert (
        '  TOOLS --> SUB["subagents.py<br/>dispatch_task()<br/>'
        'coder · code-reviewer ·<br/>explorer · researcher"]' in text
    )
    # `implementer` is a live minimal-workflow agent, so it survives there and
    # nowhere else in the doc
    head, _, minimal = text.partition("## Minimal workflow")
    assert "implementer" not in head
    assert "the `implementer` agent reads the spec" in minimal


def test_architecture_data_files_table_names_the_shipped_writers_and_readers() -> None:
    text = ARCH.read_text()
    flat = " ".join(text.split())
    assert (
        "The columns list the **principal** writers and readers, not an exhaustive census" in flat
    )
    for row in (
        "| `artifacts/spec/{id}.md` | threshold (draft) → spec-finalizer (final) | all phases |",
        "| `artifacts/plans/{id}-plan-N.md` | plan-writer | task-lead, plan-reviewer, "
        "gap-check, batch-code-reviewer, deferred-sweep, summarizer |",
        "| `artifacts/summary/{id}.md` | summarizer | finalize, `imp show` |",
        "| `artifacts/verify/{id}.md` | the `verify` / `converge-verify` / "
        "`integration-verify` gates | `imp show` |",
        "| `deferred.md` | task-lead, deferred-sweep | gap-check, deferred-sweep, finalize |",
    ):
        assert row in text, row
    # the last two retired names lived only in this table, unbackticked
    for retired in ("spec-writer", "plan-cycle-check"):
        assert retired not in text, retired


def _shipped_phase_and_step_names() -> tuple[list[str], list[str]]:
    wf = yaml.safe_load(DEFAULT_WORKFLOW.read_text())
    return (
        [p["name"] for p in wf["phases"]],
        [s["name"] for p in wf["phases"] for s in p["steps"]],
    )


def _default_workflow_section(text: str) -> str:
    return text.split("## Default workflow", 1)[1].split("\n## ", 1)[0]


def _undocumented_steps(step_names: list[str], section: str) -> list[str]:
    # `verify`/`finalize` are substrings of longer step names, so a bare-word check would
    # be satisfied by `converge-verify`/`plan-finalized`; demand the backticked span
    ambiguous = {n for n in step_names for other in step_names if n != other and n in other}
    return [n for n in step_names if (f"`{n}`" if n in ambiguous else n) not in section]


def test_architecture_documents_every_shipped_phase_and_step() -> None:
    phases, steps = _shipped_phase_and_step_names()
    # a parse that silently yielded nothing would make the whole guard vacuous
    assert {"threshold", "check-next-plan", "merge-to-base"} <= set(steps)
    text = ARCH.read_text()
    # exactly the shipped phases, as name-titled headings, in workflow order
    assert re.findall(r"(?m)^### Phase (.+)$", text) == phases
    assert _undocumented_steps(steps, _default_workflow_section(text)) == []


def test_step_guard_flags_a_step_the_doc_never_mentions() -> None:
    section = _default_workflow_section(ARCH.read_text())
    _, steps = _shipped_phase_and_step_names()
    assert _undocumented_steps([*steps, "brand-new-step"], section) == ["brand-new-step"]
    # the longer name never satisfies the shorter one's check
    assert _undocumented_steps(
        ["verify", "converge-verify", "finalize", "plan-finalized"],
        "converge-verify runs, then plan-finalized",
    ) == ["verify", "finalize"]


def _retired_names_present(text: str) -> list[str]:
    return [
        n for n in RETIRED_AGENT_NAMES if re.search(rf"(?<![\w-]){re.escape(n)}(?![\w-])", text)
    ]


def test_architecture_names_no_retired_agent() -> None:
    assert _retired_names_present(ARCH.read_text()) == []
    # boundaries treat `-` as a word character, so the live names never trip the ban
    assert _retired_names_present("the `integrator` resolves conflicts") == ["integrator"]
    assert _retired_names_present("`integration-review` and `code-reviewer` run") == []
    # a plain-substring check would flag both of these; the lookarounds must not
    assert _retired_names_present("integrators and spec-writers") == []
    # only the `-` in the lookarounds keeps a hyphen-extended name from tripping the ban
    assert _retired_names_present("`spec-writer-v2` and `pre-spec-writer` shipped") == []


def test_architecture_carries_no_circled_numerals() -> None:
    assert [c for c in "①②③④⑤⑥" if c in ARCH.read_text()] == []


def _phase_section(text: str, phase: str) -> str:
    return text.split(f"### Phase {phase}", 1)[1].split("\n### ", 1)[0]


def test_architecture_states_the_artifacts_means_committed_boundary() -> None:
    text = ARCH.read_text()
    assert ("| `quests/<id>/` | the quest home — `quest.json`, `state/`, `events.jsonl`, "
            "`artifacts/`, `spike/`, `progress.md`, and the `workflow/` snapshot |") in text
    assert "`artifacts/` means committed: everything under it is journaled by the engine" in text
    assert ("Throwaway spiker evidence is deliberately outside that boundary, in the sibling "
            "`spike/`, which the engine never journals — it dies with the quest home.") in text


def test_architecture_documents_lane_eviction_and_restore() -> None:
    # the phase prose is hard-wrapped; compare against a whitespace-flattened copy.
    # Flatten AFTER _phase_section, which bounds the section on "\n### ".
    low = " ".join(_phase_section(ARCH.read_text(), "integrate").lower().split())
    # eviction on a failure pause, and the two SHAs that gate the restore
    assert "evicts the merge lane" in low
    assert "restore point" in low
    assert "base branch tip" in low
    assert "quest worktree head" in low
    # the flat "holds it for the rest of the phase" claim is qualified
    assert "normally holds it" in low
    # a run that died holding the lane replays instead of restoring
    assert "replays the phase" in low
