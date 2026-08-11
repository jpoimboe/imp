"""docs/internals/architecture.md (a living doc) names the renamed artifact ref and no
longer mentions the deleted journal-wip ref or the agent-commits-via-CLI command."""

from __future__ import annotations

from pathlib import Path

ARCH = Path("docs/internals/architecture.md")


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
    assert (
        "bundled with the install — a committed `imp/workflows/default` overrides it"
        in text
    )
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
        "- **branch** (`{ }` diamonds) — a pure var router, no execution: "
        "`check-next-plan` and `wait-for-head`." in ARCH.read_text()
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
