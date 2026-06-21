from __future__ import annotations

import re
from pathlib import Path

import pytest

AGENTS_DIR = Path("src/imp/assets/workflows/imp/agents")

# Subagents (non-interactive, dispatched via `task`): the contract under test.
SUBAGENT_FILES = ["implementer.md", "quality-reviewer.md"]
# Interactive STEPS: the executor wires their resume hooks, so a turn-ending
# question is correct for them. Exempt from the no-bare-question rule.
INTERACTIVE_STEP_FILES = ["spec-writer.md", "finalize.md"]

SKILLS_DIR = Path("src/imp/assets/workflows/imp/skills")

# Falsifiability-hardening guards: each component's edit leaves a marker in a
# specific file. These are per-(path, marker) pairs spanning skills/ and direct
# agents/ reads (including quality-reviewer, already in SUBAGENT_FILES for the
# complete_step guards — reused here for a *different* marker, not re-listed for
# the same purpose). NOT a uniform sweep over SUBAGENT_FILES. A presence guard
# proves the marker exists somewhere in the file; section placement is left to
# author self-review and the reviewer pass.
FALSIFIABILITY_MARKERS = [
    (AGENTS_DIR / "code-reviewer.md", "falsifiab"),                      # C: whole-change backstop
    (AGENTS_DIR / "quality-reviewer.md", "falsifiab"),                   # C: per-task tripwire
    (AGENTS_DIR / "plan-writer.md", "falsifiab"),                        # B: anti-transcription
    (SKILLS_DIR / "test-driven-development.md", "plan-echo"),            # A: anti-pattern 6
    (SKILLS_DIR / "test-driven-development.md", "authoritative oracle"), # D: oracle note
    (SKILLS_DIR / "test-driven-development.md", "xfail"),                # E: xfail convention
]


ALL_AGENT_FILES = sorted(p.name for p in AGENTS_DIR.glob("*.md"))


@pytest.mark.parametrize("name", ALL_AGENT_FILES)
def test_no_hardcoded_change_dir_literal(name: str) -> None:
    text = (AGENTS_DIR / name).read_text()
    # `.imp/change` not followed by 's' — catches `.imp/change/` and the bare
    # `.imp/change` while allowing the relocated `.imp/changes/` home name.
    assert not re.search(r"\.imp/change(?!s)", text), (
        f"{name}: hardcoded '.imp/change' path — use the engine-resolved "
        "change.home variable (the {{ change.home }} template) instead")


@pytest.mark.parametrize(
    "path,marker",
    FALSIFIABILITY_MARKERS,
    ids=[f"{p.name}:{m}" for p, m in FALSIFIABILITY_MARKERS],
)
def test_falsifiability_marker_present(path: Path, marker: str) -> None:
    assert marker.lower() in path.read_text().lower(), (
        f"{path.name}: missing falsifiability marker {marker!r} — the "
        f"test-falsifiability hardening edit for this file is absent")


def _read(name: str) -> str:
    return (AGENTS_DIR / name).read_text()


@pytest.mark.parametrize("name", SUBAGENT_FILES)
def test_subagent_prompt_references_complete_step(name: str) -> None:
    assert "complete_step" in _read(name), (
        f"{name}: subagent prompt must name its terminal complete_step contract")


@pytest.mark.parametrize("name", SUBAGENT_FILES)
def test_subagent_prompt_terminal_is_not_report_back_prose(name: str) -> None:
    text = _read(name).lower()
    # The dogfood failure framing: "report back" as the terminal action with no
    # complete_step. We forbid the bare phrase "report back" entirely in
    # subagent prompts now that the contract is explicit.
    assert "report back" not in text, (
        f"{name}: drop 'report back' framing — the terminal action is complete_step")


@pytest.mark.parametrize("name", SUBAGENT_FILES)
def test_subagent_prompt_forbids_turn_ending_question(name: str) -> None:
    text = _read(name).lower()
    for banned in ("end your turn with the question",
                   "ok to pause and clarify"):
        assert banned not in text, (
            f"{name}: subagents have no user channel — remove {banned!r}")


def test_spec_writer_infers_intent_from_pasted_context() -> None:
    text = _read("spec-writer.md")
    assert "context instead of intent" in text
    assert "wait_for_user" in text
    assert "infer" in text.lower()


TURN_TERMINAL_REVIEWERS = [
    "code-reviewer.md", "quality-reviewer.md", "plan-reviewer.md",
    "spec-reviewer.md", "plan-cycle-check.md",
]


@pytest.mark.parametrize("name", TURN_TERMINAL_REVIEWERS)
def test_reviewer_md_has_no_unreconciled_no_preamble_directive(name: str) -> None:
    # The base persona tells every agent to "state in one sentence what you're
    # about to do" before its first tool call. A reviewer that ALSO carried a
    # "no preamble / output only the tool" directive would read ambiguously
    # against that base (spec acceptance #5). The planning audit found none; this
    # guard fails loudly if a future edit introduces one without reconciling it.
    low = _read(name).lower()
    for banned in ("no preamble", "without any preamble", "without preamble",
                   "do not output any text", "output only the tool",
                   "go straight to the tool"):
        assert banned not in low, (
            f"{name}: {banned!r} conflicts with the base persona's "
            f"say-one-sentence-before-your-first-tool-call default — reconcile it")


@pytest.mark.parametrize("name", INTERACTIVE_STEP_FILES)
def test_interactive_steps_exist_and_are_exempt(name: str) -> None:
    # Guard the exemption: if these files are renamed/removed, this test fails
    # loudly so the scope guard stays honest. (Skip if the file is absent in a
    # given workflow revision.)
    path = AGENTS_DIR / name
    if not path.exists():
        pytest.skip(f"{name} not present in this workflow revision")
    assert path.read_text()  # non-empty


TASK_LEAD = AGENTS_DIR / "task-lead.md"


def test_task_lead_gates_nits_on_fixability_not_severity() -> None:
    low = TASK_LEAD.read_text().lower()
    # Critical/Major stay unconditional; Minor/Nit gate on fixability (AC 1-2).
    assert "critical/major are mandatory" in low
    assert "gate on fixability, not severity" in low
    assert "fixability bar" in low
    for fix_now in ("mechanical", "confined to the task's own diff",
                    "behavior-preserving or test-strengthening", "bounded"):
        assert fix_now in low, f"missing fix-now property: {fix_now}"
    for defer in ("needs judgment or design", "out of scope", "non-local"):
        assert defer in low, f"missing defer property: {defer}"


def test_task_lead_nit_fix_dispatch_has_overreach_guardrail_and_batches() -> None:
    low = TASK_LEAD.read_text().lower()
    # Over-reach guardrail on the nit-fix dispatch (AC 3).
    assert "apply only these named nits" in low
    assert "do not refactor beyond the named change" in low
    # Qualifying nits ride the same fixup round, folded by cleanup (AC 4).
    assert "same fixup round" in low


def test_task_lead_nit_only_fixup_skips_rereview_and_degraded() -> None:
    low = TASK_LEAD.read_text().lower()
    # A nit-only fixup is cheap and polish-only (AC 5).
    assert "does not re-trigger the quality-reviewer" in low
    assert "does not mark the run degraded" in low


def test_task_lead_records_fixed_inline_nits_and_reserves_followups() -> None:
    low = TASK_LEAD.read_text().lower()
    # Qualifying nits are recorded as fixed-inline (AC 6).
    assert "fixed-inline" in low
    # The Deferred Followups list is reserved for judgment/out-of-scope findings.
    assert "deferred followups" in low
    assert "reserved for the judgment-call" in low
    # A bar-cleared nit is polish, not an open_items / degraded trigger.
    assert "cleared the fixability bar" in low


def test_task_lead_pins_deferred_followups_line_format() -> None:
    text = TASK_LEAD.read_text()
    low = text.lower()
    assert "- [ ] DEFERRED" in text
    assert "[scope:" in text
    assert "task-lead:" in text
    assert "deferred-sweep" in low      # the sweep, not the human, is the consumer


def test_task_lead_defers_to_empowered_sweep() -> None:
    low = TASK_LEAD.read_text().lower()
    # The defer rationale routes to the empowered sweep, not the human (AC 5).
    assert "empowered" in low, "missing 'empowered' in task-lead defer bullet"
    assert "may fix" in low, "missing 'may fix' in task-lead defer bullet"
    # The existing defer properties stay — the sweep is the consumer that takes
    # them up.
    for defer in ("out of scope", "non-local"):
        assert defer in low, f"missing defer property: {defer}"


FINALIZE = AGENTS_DIR / "finalize.md"
SWEEP = AGENTS_DIR / "deferred-sweep.md"


def test_deferred_sweep_crossrefs_terminal_gate_dispositions() -> None:
    low = SWEEP.read_text().lower()
    # The two terminal markers finalize writes are inert to the sweep (like dismissed).
    assert "seeded <change" in low
    assert "ignored-at-gate" in low


def test_finalize_reads_sweep_dispositions() -> None:
    low = FINALIZE.read_text().lower()
    # Reads the new dispositions: surfaces needs-decision, rolls up fixed,
    # ignores dismissed.
    assert "needs-decision" in low
    assert "— fixed" in low        # the sweep's output format, counted into built rollup
    assert "dismissed" in low      # named only to exclude it
    # The old per-fix surfacing/veto vocabulary is gone (AC 4).
    for stale in ("fyi-fixed", "still-deferred", "silent-fixed", "veto"):
        assert stale not in low, f"stale finalize vocabulary: {stale!r}"


def test_finalize_counts_opportunistic_from_completed_plans() -> None:
    low = FINALIZE.read_text().lower()
    # built carries a one-line rollup counting the opportunistic fixes...
    assert "opportunistic improvement" in low
    # ...counted from the durable ## Completed plans section, NOT the transient list.
    assert "## completed plans" in low



def test_finalize_prompt_states_merge_gate_affordance() -> None:
    low = FINALIZE.read_text().lower()
    assert "merge gate" in low
    assert "rework" in low
    assert "reply" in low


def test_finalize_walks_open_items_one_at_a_time() -> None:
    low = FINALIZE.read_text().lower()
    # Per-item walk with a recommendation, one item per solicit.
    assert "one at a time" in low or "one item per" in low
    assert "recommendation" in low
    assert "fix now" in low
    assert "create_change" in low
    assert "seeded" in low
    assert "ignored-at-gate" in low
    # Prose, not a labelled field-dump.
    assert "prose" in low
    assert "field-dump" in low


def test_finalize_has_no_fyi_or_veto_vocabulary() -> None:
    low = FINALIZE.read_text().lower()
    for stale in ("fyi", "veto", "fyi-fixed", "silent-fixed", "still-deferred"):
        assert stale not in low, f"reintroduced stale finalize vocabulary: {stale!r}"


PLAN_CYCLE_CHECK = AGENTS_DIR / "plan-cycle-check.md"


def test_plan_cycle_check_counts_tasks_from_plan_file() -> None:
    low = PLAN_CYCLE_CHECK.read_text().lower()
    # Counts the just-implemented plan's tasks from the PLAN FILE, not progress.md.
    assert "running_tasks" in low
    assert "{{ vars.tasks_since_review }}".lower() in low
    assert "plan file" in low
    assert "## task" in low
    # Emits the running total so the workflow counter advances.



def test_plan_cycle_check_owns_coverage_no_plan_gap_reopen() -> None:
    text = PLAN_CYCLE_CHECK.read_text()
    # The review can no longer re-open the cycle via plan_gap; that sentence is gone.
    assert "plan_gap" not in text
    assert "re-open the cycle" not in text
    # It is the SOLE completeness guard now.
    assert "sole" in text.lower() and "completeness" in text.lower()


CODE_REVIEWER = AGENTS_DIR / "code-reviewer.md"


def test_task_lead_collapses_completed_plans_into_bounded_section() -> None:
    text = TASK_LEAD.read_text()
    low = text.lower()
    # progress.md has a durable, bounded ## Completed plans section.
    assert "## Completed plans" in text
    assert "one line per plan" in low
    # On done, collapse the current plan and drop its verbose per-task narrative.
    assert "collapse" in low
    assert "drop the verbose per-task" in low or "drop the per-task narrative" in low
    # The current plan's per-task commit subjects are still kept WHILE implementing
    # (needed to re-find a buried task for a fixup).
    assert "commit subject" in low


def test_plan_writer_states_soft_task_size_target() -> None:
    text = (AGENTS_DIR / "plan-writer.md").read_text()
    low = text.lower()
    # The soft target is stated in TASKS (not lines of code), with the split-and-
    # supercycle guidance, and explicitly cross-references the hard ceiling gate.
    assert "5\u20138 tasks" in text
    assert "natural dependency boundaries" in low
    assert "supercycle" in low
    assert "task headings" in low
    assert "not lines of code" in low


def test_task_lead_documents_verify_triage() -> None:
    text = (AGENTS_DIR / "task-lead.md").read_text()
    low = text.lower()
    assert "tolerated-failures.md" in text          # the change-local record path
    assert "verify_tolerated" in text               # the signal it emits
    assert "attributable" in low                     # the single attributability judgment
    assert "review_base..HEAD" in text               # judged against the diff
    # The two-category taxonomy is gone — no tags, no "green at baseline", no union set.
    assert "[baseline]" not in text
    assert "[flaky]" not in text
    assert "green at baseline" not in low
    # The max_loops pause meaning is documented (non-converging triage).
    assert "max_loops" in text
    assert "non-converging" in low
    assert "commit-check" not in text               # the commit-check step is deleted
    assert "consume the budget" not in low          # the carry-era budget wording is gone


def test_code_reviewer_resolves_diff_range_per_cadence() -> None:
    low = CODE_REVIEWER.read_text().lower()
    # The reviewer resolves its own range from the review cadence, not from a
    # coordinator dispatch: per-batch over review_base, end-of-change over cur_base_head.
    assert "review_scope" in low
    assert "review_base" in low                 # per-batch range
    assert "cur_base_head" in low               # end-of-change range from recorded fork
    assert "git merge-base" not in low
    assert "pure advisor" not in low            # it routes now
    assert "final whole-change review" not in low


def test_finalize_surfaces_tolerated_failures_as_needs_decision() -> None:
    text = (AGENTS_DIR / "finalize.md").read_text()
    low = text.lower()
    assert "tolerated-failures.md" in text
    assert "<signature>: <justification>" in text    # the untagged entry shape
    assert "as its own" in text  # each entry surfaced individually
    assert "[baseline]" not in text
    assert "[flaky]" not in text
    # The max_loops pause meaning is documented (non-converging triage).
    assert "max_loops" in text
    assert "non-converging" in low


PLAN_WRITER = AGENTS_DIR / "plan-writer.md"


def test_plan_writer_rework_authors_corrective_n_plus_1() -> None:
    text = PLAN_WRITER.read_text()
    low = text.lower()
    # A post-settlement rework authors a NEW corrective plan, never revises the
    # settled plan in place.
    assert "corrective plan" in low
    assert "never revise the settled plan" in " ".join(low.split())
    # The removed override no longer claims a rework reuses ("bounces") the same plan.
    assert "rework bounce" not in low


def test_plan_writer_deviation_bounded_to_pending_tasks() -> None:
    text = PLAN_WRITER.read_text()
    low = text.lower()
    assert "deviation revise mode" in low
    # A deviation revise touches only pending tasks, below the progress.md boundary.
    assert "bounded to pending tasks" in low
    assert "completion boundary" in low
    # A deviation implicating an already-implemented task escalates to append-N+1.
    assert "escalate to the append-n+1 path" in low


def test_spec_writer_surfaces_needs_decision_in_reader_form() -> None:
    text = _read("spec-writer.md")
    assert "Deferred Followups" in text
    assert "— needs-decision:" in text          # finalize's reader-form marker (em dash U+2014)
    assert "DEFERRED spec-writer:" in text       # tagged with its own source
    assert "fast_path" in text


def test_plan_writer_surfaces_needs_decision_in_reader_form() -> None:
    text = PLAN_WRITER.read_text()
    assert "Deferred Followups" in text
    assert "— needs-decision:" in text
    assert "DEFERRED plan-writer:" in text
    assert "fast_path" in text


def test_deferred_sweep_passes_through_pre_classified_needs_decision() -> None:
    text = (AGENTS_DIR / "deferred-sweep.md").read_text()
    low = text.lower()
    assert "pre-classified human" in low       # recognizes the spec/plan-authored marker
    assert "leave the line exactly as-is" in low  # the pass-through instruction
    assert "never dismiss it" in low            # passed through, not risk-dismissed


def test_finalize_presents_spec_plan_authored_needs_decision() -> None:
    text = (AGENTS_DIR / "finalize.md").read_text()
    low = text.lower()
    assert "spec-writer" in low and "plan-writer" in low
    assert "fast_path" in low or "fast path" in low


def test_deferred_sweep_names_code_reviewer_as_producer() -> None:
    text = SWEEP.read_text()
    assert "code-reviewer append it" in text
    assert "review-panel" not in text


def test_deferred_sweep_documents_verify_triage() -> None:
    text = SWEEP.read_text()
    low = text.lower()
    # The change-local record and the signal it emits.
    assert "tolerated-failures.md" in text
    assert "sweep_tolerated: true" in text
    assert "sweep_tolerated: false" in text
    # The single attributability judgment, against the diff.
    assert "attributable" in low
    assert "review_base..HEAD" in text
    # The two-category taxonomy is gone.
    assert "[baseline]" not in text
    assert "[flaky]" not in text
    assert "green at baseline" not in low
    # Feedback-gated: a fresh entry drains the list; a resumed (sweep-verify) entry
    # does triage only and does NOT re-drain.
    assert "injected feedback" in low
    assert "triage only" in low
    # A real regression is fixed with a PLAIN commit, never a --fixup: a sweep fix is its
    # own logical unit (a deferred item), so it lands as its own commit, folded/stamped by
    # cleanup-sweep.
    assert 'git commit -m' in text
    assert "--fixup=HEAD" in text
    # The max_loops pause meaning is documented (non-converging triage).
    assert "max_loops" in text
    assert "non-converging" in low
    assert "cleanup-sweep" in text                  # the sweep's cleanup gate
    assert "consume the budget" not in low          # the carry-era budget wording is gone


def test_task_lead_and_deferred_sweep_marked_synced_pair() -> None:
    marker = "SYNCED PAIR (task-lead ↔ deferred-sweep)"
    assert marker in TASK_LEAD.read_text()
    assert marker in SWEEP.read_text()


INTEGRATION_RESOLVE = AGENTS_DIR / "integration-resolve.md"


def test_integration_resolve_discriminates_bypass_via_query_events() -> None:
    text = INTEGRATION_RESOLVE.read_text()
    low = text.lower()
    # Discriminate the two bypass reasons from the journal, keyed on the routing
    # action.target == "integration-resolver" (the events carry no verdict/exit field).
    assert "query_events" in text
    assert "step_completed" in text
    assert "action.target" in text
    assert "integration-resolver" in text
    assert "most recent" in low
    assert "integration-reviewer" in text         # breakage gate
    assert "integration-verify" in text          # verify-fail gate
    # Ambiguous/indeterminate journal -> solicit, never auto-merge.
    assert "ambiguous" in low or "indeterminate" in low


def test_integration_resolve_breakage_always_solicits() -> None:
    low = INTEGRATION_RESOLVE.read_text().lower()
    # A breakage bypass ALWAYS solicits and is never auto-merged, regardless of verify state.
    assert "breakage" in low
    assert "always" in low
    assert "never auto-merge" in low or "never auto-merged" in low


def test_integration_resolve_verify_fail_honor_existing_auto_merge() -> None:
    text = INTEGRATION_RESOLVE.read_text()
    low = text.lower()
    # On a verify-fail bypass, reproduce the failure set by running the injected
    # $IMP_VERIFY_COMMAND (the gate output does not survive; its relative
    # ./scripts/run-verify.sh path does not resolve from the agent's project-root cwd).
    assert "IMP_VERIFY_COMMAND" in text
    assert "tolerated-failures.md" in text
    # All current failures within baseline+flaky -> decide merge WITHOUT wait_for_user.
    assert "WITHOUT" in text and "wait_for_user" in text
    # It honors only the EXISTING set; it never writes a new tolerated entry.
    assert "never write" in low or "never writes" in low
    assert "new tolerated" in low
    # Else branch: any failure outside the tolerated set -> solicit the human.
    assert "solicit" in low
    # It now refers to "the recorded tolerated set", not the old two-category union notation.
    assert "recorded tolerated set" in low
    assert "[baseline]" not in text
    assert "[flaky]" not in text


def test_integration_prompts_use_recorded_endpoints() -> None:
    review = (AGENTS_DIR / "integration-review.md").read_text()
    resolve = (AGENTS_DIR / "integration-resolve.md").read_text()
    integ = (AGENTS_DIR / "integrator.md").read_text()
    # Consumers parse the labeled endpoint lines, not a post-rebase merge-base diff.
    for text in (review, resolve):
        assert "Integration-fork:" in text
        assert "Integration-base:" in text
        assert 'git diff "$(git merge-base "$BASE" HEAD)" "$BASE"' not in text
    # The integrator re-records the endpoints and replants cur_base_head.
    assert "Integration-fork:" in integ
    assert "Integration-base:" in integ
    assert "cur_base_head" in integ
