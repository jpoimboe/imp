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


def test_architecture_names_the_out_of_workflow_setup_agent() -> None:
    text = ARCH.read_text()
    assert "`assets/init.md`" in text
    assert "bundled_assets_dir()" in text


def test_docs_reorg_moved_all_eight_with_no_stale_root_copy() -> None:
    moved = {
        "docs/what-is-imp.md": "docs/guide/what-is-imp.md",
        "docs/imp-operator-notes.md": "docs/guide/operator-notes.md",
        "docs/architecture.md": "docs/internals/architecture.md",
        "docs/imp-conventions.md": "docs/internals/conventions.md",
        "docs/imp-invariants.md": "docs/internals/invariants.md",
        "docs/subagent-authoring.md": "docs/internals/subagent-authoring.md",
        "docs/tdd-analysis-report.md": "docs/internals/tdd-analysis-report.md",
        "docs/tdd-brittleness-verdict.md": "docs/internals/tdd-brittleness-verdict.md",
    }
    for old, new in moved.items():
        assert not Path(old).exists(), f"stale root copy left: {old}"
        assert Path(new).is_file(), f"missing at new home: {new}"


def test_architecture_documents_the_out_of_checkout_state_directory() -> None:
    text = ARCH.read_text()
    assert "### Where state lives" in text
    assert "`~/.imp/projects/<slug>/`" in text
    assert "project_state_dir()" in text
    assert "$IMP_STATE_DIR" in text
    assert "$IMP_HOME" in text
    assert "`repo` marker" in text
    for entry in ("`quests/<id>/`", "`worktrees/<id>/`", "`archive/`"):
        assert entry in text, entry
    # the one imp path left in a worktree is the symlink into the quest home
    assert "`.imp/quests/<id>` pointing at that quest's home" in text
    # no checkout-relative state path survives anywhere in the doc
    for stale in (".imp/quest/", ".imp/archive", ".imp/worktrees"):
        assert stale not in text, stale


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
  IRV -->|"blocking / minors"| REC
  IRV -. Error .-> HUMAN
  IRV --> SUM["summarizer"]
  SUM --> FIN["finalize<br/>HUMAN: merge / rework / pause"]
  FIN -->|rework| back([→ plan-writer])
  FIN -. pause .-> HUMAN
  FIN -->|merge| CM{{"cleanup-merge"}}
  CM -->|"fixup conflict"| ASR["autosquash-resolve-merge"]
  ASR --> CM
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
    assert "change" not in text
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
        "| `artifacts/summary/{id}.md` | summarizer — or threshold itself on an "
        "express quest | finalize, `imp show` |",
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


def test_architecture_carries_no_circled_numerals() -> None:
    assert [c for c in "①②③④⑤⑥" if c in ARCH.read_text()] == []
