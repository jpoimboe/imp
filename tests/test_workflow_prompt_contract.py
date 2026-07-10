from __future__ import annotations

import re
from pathlib import Path

import pytest

from imp.agents.prompts import PromptRenderError, build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog

AGENTS_DIR = Path("src/imp/assets/workflows/imp/agents")

# Subagents (non-interactive, dispatched via `task`): the contract under test.
SUBAGENT_FILES = ["coder.md", "code-reviewer.md"]
# Interactive STEPS: the executor wires their resume hooks, so a turn-ending
# question is correct for them. Exempt from the no-bare-question rule.
INTERACTIVE_STEP_FILES = ["threshold.md", "spec-finalizer.md", "finalize.md"]

SKILLS_DIR = Path("src/imp/assets/workflows/imp/skills")

# Falsifiability-hardening guards: each component's edit leaves a marker in a
# specific file. These are per-(path, marker) pairs spanning skills/ and direct
# agents/ reads (including code-reviewer, already in SUBAGENT_FILES for the
# no-complete_step guard — reused here for a *different* marker, not re-listed for
# the same purpose). NOT a uniform sweep over SUBAGENT_FILES. A presence guard
# proves the marker exists somewhere in the file; section placement is left to
# author self-review and the reviewer pass.
FALSIFIABILITY_MARKERS = [
    (AGENTS_DIR / "batch-code-reviewer.md", "falsifiab"),                # C: whole-change backstop
    (AGENTS_DIR / "code-reviewer.md", "falsifiab"),                      # C: per-task tripwire
    (AGENTS_DIR / "plan-writer.md", "falsifiab"),                        # B: anti-transcription
    (SKILLS_DIR / "agent" / "test-driven-development.md", "plan-echo"),        # A: anti-pattern 6
    (SKILLS_DIR / "agent" / "test-driven-development.md", "authoritative oracle"), # D: oracle note
    (SKILLS_DIR / "agent" / "test-driven-development.md", "xfail"),          # E: xfail convention
]


ALL_AGENT_FILES = sorted(p.name for p in AGENTS_DIR.glob("*.md"))


@pytest.mark.parametrize("name", ALL_AGENT_FILES)
def test_no_agent_prompt_references_tolerated_failures_file(name: str) -> None:
    assert "tolerated-failures.md" not in (AGENTS_DIR / name).read_text(), (
        f"{name}: references the retired tolerated-failures.md record — "
        "tolerations are ordinary Deferred Followups needs-decision lines now")


@pytest.mark.parametrize("name", ALL_AGENT_FILES)
def test_no_hardcoded_quest_dir_literal(name: str) -> None:
    text = (AGENTS_DIR / name).read_text()
    # `.imp/change` not followed by 's' — catches `.imp/change/` and the bare
    # `.imp/change` while allowing the relocated `.imp/quests/` home name.
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
def test_subagent_prompt_has_no_complete_step(name: str) -> None:
    assert "complete_step" not in _read(name), (
        f"{name}: a dispatched subagent has no complete_step tool — its final "
        "message is its entire deliverable")


@pytest.mark.parametrize("name", SUBAGENT_FILES)
def test_subagent_prompt_terminal_is_not_report_back_prose(name: str) -> None:
    text = _read(name).lower()
    # The dogfood failure framing: "report back" as the terminal action with no
    # complete_step. We forbid the bare phrase "report back" entirely in
    # subagent prompts now that the contract is explicit.
    assert "report back" not in text, (
        f"{name}: drop 'report back' framing — the terminal action is the subagent's final message")


@pytest.mark.parametrize("name", SUBAGENT_FILES)
def test_subagent_prompt_forbids_turn_ending_question(name: str) -> None:
    text = _read(name).lower()
    for banned in ("end your turn with the question",
                   "ok to pause and clarify"):
        assert banned not in text, (
            f"{name}: subagents have no user channel — remove {banned!r}")


def test_threshold_infers_intent_from_pasted_context(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    # Pasted material arrives as the opening message, so it renders in the seeded
    # ({% if run.opening %}) branch. The threshold prompt must still tell the
    # agent to infer intent from the substance and ban the classification
    # recital — never blankly re-ask.
    out = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc", project_facts={},
        opening="pasted imp log for change abc"),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "Pasted material" in out
    assert "classification recital" in out
    assert "ending your turn" in out
    assert "infer" in out.lower()


TURN_TERMINAL_REVIEWERS = [
    "batch-code-reviewer.md", "code-reviewer.md", "plan-reviewer.md",
    "spec-reviewer.md", "gap-check.md",
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
    assert "does not re-trigger the code-reviewer" in low
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
    assert "— fixed" in low        # the sweep's output format, excluded from open_items
    assert "dismissed" in low      # named only to exclude it
    # The old per-fix surfacing/veto vocabulary is gone (AC 4).
    for stale in ("fyi-fixed", "still-deferred", "silent-fixed", "veto"):
        assert stale not in low, f"stale finalize vocabulary: {stale!r}"


def test_finalize_prompt_states_merge_gate_affordance() -> None:
    low = FINALIZE.read_text().lower()
    assert "merge gate" in low
    assert "rework" in low
    assert "natural-language response" in low


def test_finalize_walks_open_items_fix_defer_ignore() -> None:
    low = FINALIZE.read_text().lower()
    # The fix / defer / ignore three-outcome walk, each item with a recommendation —
    # but the one-item-per-turn cadence is gone; the agent drives the conversation.
    assert "one at a time" not in low
    assert "one item per turn" not in low
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
        assert re.search(rf"\b{re.escape(stale)}\b", low) is None, (
            f"reintroduced stale finalize vocabulary: {stale!r}")


CODE_REVIEWER = AGENTS_DIR / "batch-code-reviewer.md"


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


def test_task_lead_fixes_forward_or_logs_never_jumps_back() -> None:
    text = TASK_LEAD.read_text()
    low = text.lower()
    # The backward deviation jump is gone from the body entirely.
    assert "deviation" not in low
    # Forward dispositions are documented.
    assert "resolved-forward" in low
    assert "- resolved-forward task-lead:" in text
    assert "needs-replan" in low
    # The needs-replan note is a durable checkbox in the pinned reader shape.
    assert "- [ ] NEEDS-REPLAN task-lead:" in text
    # task-lead never escalates to the human mid-implementation.
    assert "never** yield to the human" in low
    assert "wait_for_user" not in low
    # It always returns a green tree, reverting forward through the coder.
    assert "revert it forward" in low
    assert "green tree" in low
    # The notes live in the append-only deferred.md, untouched by the progress.md collapse.
    assert "survives every end-of-plan `progress.md` rewrite" in low
    # The TRANSIENT per-task narrative (dropped at collapse) must carry NO durable
    # note: step 5 records commit subjects so buried tasks can be re-found for a fixup,
    # and per-task entries are kept only while the plan is in flight.
    assert "re-found for a fixup" in low
    assert "only while" in low


def test_plan_writer_states_natural_sizing_no_floor_or_ceiling() -> None:
    text = (AGENTS_DIR / "plan-writer.md").read_text()
    low = text.lower()
    # Sizing is stated in TASKS at natural boundaries, with a full-path vs
    # express/self-scope split, no floor and no ceiling.
    assert "natural commit boundaries" in low
    assert "natural dependency boundary" in low
    assert "not lines of code" in low
    assert "express" in low and "self-scope" in low
    assert "default `plan_total = 1`" in text
    assert "outline-driven plan loop" in low
    # The ~5-8 floor, the carve anti-patterns, the per-plan-review motivation, and
    # the ~12/CEILING ceiling cross-reference are all gone.
    assert "5\u20138 tasks" not in text
    assert "fill up toward" not in low
    assert "under-filling" not in low
    assert "per-plan review" not in low
    assert "forced-atomic remainder" not in low
    assert "file family" not in low
    assert "capstone task" not in low
    assert "deferral target" not in low
    assert "task headings" not in low
    assert "~12" not in text
    assert "ceiling" not in low
    # Atomicity stays sacrosanct (stated in Task vs. Step, untouched).
    assert "atomicity hard rule" in low


def test_task_lead_documents_verify_triage() -> None:
    text = (AGENTS_DIR / "task-lead.md").read_text()
    low = text.lower()
    assert "tolerated-failures.md" not in text       # the separate record is retired
    assert "DEFERRED verify:" in text                # the gate-sourced toleration line
    assert "## Deferred Followups" in text           # recorded in the followups list
    assert "— needs-decision:" in text          # born in needs-decision state (em dash)
    assert "verify_tolerated" in text                # the signal it emits
    assert "attributable" in low                     # the single attributability judgment
    assert "cur_base_head..HEAD" in text             # judged against the diff
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
    # The reviewer always does the end-of-change pass over cur_base_head.
    assert "cur_base_head" in low               # end-of-change range from recorded fork
    assert "git merge-base" not in low
    assert "pure advisor" not in low            # it routes now
    assert "final whole-change review" not in low


def test_finalize_surfaces_tolerated_failures_as_needs_decision() -> None:
    text = (AGENTS_DIR / "finalize.md").read_text()
    low = text.lower()
    # No separate tolerated-failures record or section — tolerations are ordinary
    # needs-decision items now, surfaced through the existing walk.
    assert "tolerated-failures.md" not in text
    assert "Surface tolerated verification failures" not in text
    assert "<signature>: <justification>" not in text
    # They reach finalize as DEFERRED verify: / DEFERRED converge-verify: needs-decision lines.
    assert "DEFERRED verify:" in text
    assert "DEFERRED converge-verify:" in text
    assert "sweep-verify" not in text
    assert "needs-decision" in low
    # finalize must not re-run the failure or editorialize its current status.
    assert "already fixed" in low
    assert "re-run the failure" in low
    # The max_loops pause meaning is still documented (non-converging triage).
    assert "max_loops" in text
    assert "non-converging" in low
    assert "[baseline]" not in text
    assert "[flaky]" not in text


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


def test_plan_writer_has_no_deviation_revise_mode() -> None:
    text = PLAN_WRITER.read_text()
    low = text.lower()
    # The backward deviation-revise mode is gone entirely.
    assert "deviation revise mode" not in low
    assert "deviation-bounded revise" not in low
    assert "task-lead deviation" not in low
    # The minors-integration in-place revise survives.
    assert "minors-integration" in low
    # Authoring the covering N+1 consumes the NEEDS-REPLAN lifecycle handle.
    assert "NEEDS-REPLAN" in text
    assert "[x]" in text


def test_spec_writer_surfaces_needs_decision_in_reader_form() -> None:
    text = _read("spec-finalizer.md")
    assert "Deferred Followups" in text
    assert "— needs-decision:" in text          # finalize's reader-form marker (em dash U+2014)
    assert "DEFERRED spec-finalizer:" in text    # tagged with its own source
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
    assert "only entries with no disposition tag" in low  # drain-only-untagged invariant pinned


def test_finalize_presents_spec_plan_authored_needs_decision() -> None:
    text = (AGENTS_DIR / "finalize.md").read_text()
    low = text.lower()
    assert "spec-finalizer" in low and "plan-writer" in low
    assert "fast_path" in low or "fast path" in low


def test_deferred_sweep_names_code_reviewer_as_producer() -> None:
    text = SWEEP.read_text()
    assert "batch-code-reviewer, plan-writer, and plan-reviewer append it" in text
    assert "review-panel" not in text


def test_deferred_sweep_documents_verify_triage() -> None:
    text = SWEEP.read_text()
    low = text.lower()
    # The toleration is a Deferred Followups line sourced `converge-verify`; the
    # separate record is retired.
    assert "tolerated-failures.md" not in text
    assert "DEFERRED converge-verify:" in text
    # The single attributability judgment, against the diff.
    assert "attributable" in low
    assert "cur_base_head..HEAD" in text
    # The two-category taxonomy is gone.
    assert "[baseline]" not in text
    assert "[flaky]" not in text
    assert "green at baseline" not in low
    # Feedback-gated: a fresh entry drains the list; a resumed (converge-verify) entry
    # does triage only and does NOT re-drain.
    assert "injected feedback" in low
    assert "triage only" in low
    # A real regression is fixed with a PLAIN commit, never a --fixup: a sweep fix is its
    # own logical unit (a deferred item), so it lands as its own commit, folded/stamped
    # later by the end-of-change cleanup-merge.
    assert 'git commit -m' in text
    assert "--fixup=HEAD" in text
    # The max_loops pause meaning is documented (non-converging triage).
    assert "max_loops" in text
    assert "non-converging" in low
    assert "cleanup-merge" in text                  # fold/stamp deferred to the end-of-change gate
    assert "consume the budget" not in low          # the carry-era budget wording is gone


def test_task_lead_and_deferred_sweep_marked_synced_pair() -> None:
    marker = "SYNCED PAIR (task-lead ↔ deferred-sweep)"
    assert marker in TASK_LEAD.read_text()
    assert marker in SWEEP.read_text()


def test_integration_prompts_use_recorded_endpoints() -> None:
    review = (AGENTS_DIR / "integration-review.md").read_text()
    integ = (AGENTS_DIR / "reconciler.md").read_text()
    # Both consumers read the fork from the integration_fork var, not a labeled-line file.
    assert "vars.integration_fork" in review
    assert "Integration-fork:" not in review
    assert "Integration-base:" not in review
    assert 'git diff "$(git merge-base "$BASE" HEAD)" "$BASE"' not in review
    assert "progress.md" in review
    assert "vars.integration_fork" in integ
    assert "Integration-fork:" not in integ
    assert "cur_base_head" in integ


RUBRIC_PARTIAL = Path("src/imp/assets/workflows/imp/partials/severity.md")
RUBRIC_REVIEWERS = [
    "spec-reviewer.md", "plan-reviewer.md", "batch-code-reviewer.md",
    "code-reviewer.md", "integration-review.md",
]


@pytest.mark.parametrize("name", RUBRIC_REVIEWERS)
def test_reviewer_includes_canonical_severity_partial(name: str) -> None:
    assert '{% include "partials/severity.md" %}' in (AGENTS_DIR / name).read_text(), (
        f"{name}: must reference the single-source severity partial, not a "
        f"hand-written severity-definition block")


def test_severity_partial_holds_canonical_definitions() -> None:
    text = RUBRIC_PARTIAL.read_text()
    for term in ("**Critical**", "**Major**", "**Minor**", "**Nit**"):
        assert term in text, term
    assert "behavioral impact, not file type" in text
    assert "non-trivial risk" in text


def test_no_agent_prompt_mentions_breakage_or_bypass_gate() -> None:
    for md in AGENTS_DIR.glob("*.md"):
        text = md.read_text().lower()
        assert "breakage" not in text, md.name
        assert "bypass gate" not in text, md.name


def test_spec_writer_recognizes_threshold_handoff() -> None:
    body = _read("spec-finalizer.md")
    # The handoff opening and its adopt-with-light-review section are named.
    assert "Adopt the spec drafted with the user and finalize it:" in body
    assert "Threshold handoff" in body
    # head_changed is re-derived from the two persisted heads, not the tool return.
    assert "change.cur_base_head" in body
    assert "change.orig_base_head" in body


NO_FIXUP_RULE_AGENTS = ["task-lead.md", "deferred-sweep.md", "reconciler.md"]


@pytest.mark.parametrize("name", NO_FIXUP_RULE_AGENTS)
def test_committing_agents_carry_no_fixup_outside_change_rule(name: str) -> None:
    # The standing rule: never --fixup/--squash a commit the change does not own; a
    # base-traced failure is fixed forward as a new commit. (The surviving-directive
    # bounce is retired: the single end-of-change cleanup pauses on an unfoldable
    # directive rather than bouncing it back to a committing agent.)
    low = _read(name).lower()
    assert "does not own" in low
    assert "forward" in low


def test_spec_writer_handoff_renders_drift_branch_on_moved_base(tmp_path) -> None:
    body = _read("spec-finalizer.md")
    log = JsonlEventLog(tmp_path / "events.jsonl")
    moved = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc",
        cur_base_head="NEW", orig_base_head="OLD",
        project_facts={}),
        workflow_dir=bundled_workflow_dir("imp")).lower()
    # Key on a phrase unique to the handoff true-branch.
    assert "the base branch advanced since the draft started" in moved   # head_changed=true
    # Drift reconciles silently in-turn; the branch carries no yield encouragement —
    # only a genuine intent-fork parks, via the Surfacing-a-decision machinery below.
    assert "reconcile it yourself and proceed; do not yield for it" in moved
    assert "yield — by ending your turn with the question" not in moved
    assert "adopt near-silently" not in moved

    same = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc",
        cur_base_head="SAME", orig_base_head="SAME",
        project_facts={}),
        workflow_dir=bundled_workflow_dir("imp")).lower()
    assert "adopt near-silently" in same           # head_changed=false branch
    assert "the base branch advanced since the draft started" not in same

    # Autonomy no longer changes this branch — the drift-reconcile prompt is identical
    # whether or not the run is autonomous (the conditional was removed).
    autonomous_moved = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc",
        cur_base_head="NEW", orig_base_head="OLD",
        autonomous=True,
        project_facts={}),
        workflow_dir=bundled_workflow_dir("imp")).lower()
    assert autonomous_moved == moved


HUMAN_DECISION_PARTIAL = Path(
    "src/imp/assets/workflows/imp/partials/human-decision.md")


def test_human_decision_partial_holds_canonical_bar() -> None:
    text = HUMAN_DECISION_PARTIAL.read_text()
    # The two conjoined conditions.
    assert "intent-level" in text
    assert "costly to reverse" in text
    assert "Both conditions must hold" in text
    # Observable-behavior / public-contract is NOT a standalone trigger; the
    # suite is the backstop for a covered behavior change.
    assert "test suite is the backstop" in text
    assert "not on its own a reason to escalate" in text.lower()
    # The decide-yourself-and-log directive.
    assert "decide yourself and log" in text
    # The "make it the right call, not the cheap one" quality clause. The
    # canonical sentence wraps across a newline in the partial, and read_text()
    # preserves that raw newline, so a continuous-string assert spanning the
    # break would never match — assert within-line substrings instead.
    assert "Reversibility lowers the bar for bothering" in text
    assert "never the bar for code quality." in text
    assert "not the smallest diff" in text


def test_deferred_sweep_sources_human_decision_bar() -> None:
    text = (AGENTS_DIR / "deferred-sweep.md").read_text()
    # Sources the shared bar.
    assert '{% include "partials/human-decision.md" %}' in text
    # The standalone observable-behavior trigger and the risk-paragraph bar
    # definition are gone (no longer its own prose definition of the bar).
    assert "observable/external behavior" not in text
    assert "alters observable behavior" not in text
    assert "preserves observable behavior" not in text
    # Agent-specific framing survives: the fail-fast guard under `fixed`, the
    # `dismissed` disposition, and the fix-it-or-dismiss nuance.
    assert "fail-fast" in text
    assert "**dismissed**" in text
    assert '"is this cheap change worth it?"' in text


def test_spec_finalizer_sources_human_decision_bar() -> None:
    text = (AGENTS_DIR / "spec-finalizer.md").read_text()
    assert '{% include "partials/human-decision.md" %}' in text
    # The inline bar prose is gone.
    assert "an ambiguity you had to assume past" not in text
    # Mechanics preserved: reader-form line + fast_path note.
    assert "DEFERRED spec-finalizer:" in text
    assert "fast_path" in text


def test_reconciler_sources_human_decision_bar() -> None:
    text = (AGENTS_DIR / "reconciler.md").read_text()
    assert '{% include "partials/human-decision.md" %}' in text
    # Reader-form line + asynchronous-triage framing preserved.
    assert "DEFERRED reconciler:" in text
    assert "asynchronous human triage" in text


def test_threshold_adopts_discovered_work_by_default(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "threshold"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    attended = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc", project_facts={}),
        workflow_dir=bundled_workflow_dir("imp"))
    # Adoption is the unmarked default: discovered work folds into the current change.
    assert "## Keep discovered work here by default" in attended
    assert "fold it into this change" in attended
    # Spin-off is demoted to the exception, not a first-class move.
    assert "is the exception" in attended
    # The create_change capability survives, in condensed conditional form.
    assert "create_change" in attended
    assert 'depends_on=["abc"]' in attended
    assert "imp resume" in attended
    # The old spin-off-first section and its framing are gone.
    assert "## Splitting off a separate change" not in attended
    assert "forcing every idea" not in attended
    assert "before calling `create_change`" not in attended

    # Autonomous runs omit the whole {% if not run.autonomous %} block; because
    # create_change appears nowhere else in threshold.md, its absence proves the
    # block was dropped.
    autonomous = render_prompt(body, build_context(
        quest_title="t", event_log=log, quest_id="abc",
        quest_home=".imp/quests/abc", autonomous=True,
        project_facts={}),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "## Keep discovered work here by default" not in autonomous
    assert "create_change" not in autonomous


def test_task_lead_sources_human_decision_bar() -> None:
    text = (AGENTS_DIR / "task-lead.md").read_text()
    assert '{% include "partials/human-decision.md" %}' in text
    # The needs-decision note-type row and its routing semantics survive.
    assert "**needs-decision**" in text
    assert "gate's needs-decision walk" in text
    # The include must not reintroduce the removed backward-jump vocabulary.
    assert "deviation" not in text.lower()


def test_plan_writer_sources_human_decision_bar() -> None:
    text = (AGENTS_DIR / "plan-writer.md").read_text()
    assert '{% include "partials/human-decision.md" %}' in text
    # The inline "Surfacing a decision you cannot resolve" bar prose is gone.
    assert "an ambiguity you had to assume past" not in text
    # Mechanics preserved: reader-form line + fast_path note.
    assert "DEFERRED plan-writer:" in text
    assert "fast_path" in text
    # The distinct higher-bar Ask-vs-decide / wait_for_user framing stays.
    assert "When to ask — all three must hold" in text


HUMAN_DECISION_AGENTS = [
    "deferred-sweep.md", "spec-finalizer.md", "reconciler.md",
    "task-lead.md", "plan-writer.md",
]


@pytest.mark.parametrize("name", HUMAN_DECISION_AGENTS)
def test_authoring_agent_includes_human_decision_once(name: str) -> None:
    text = (AGENTS_DIR / name).read_text()
    assert text.count('{% include "partials/human-decision.md" %}') == 1, (
        f"{name}: must source the shared human-decision bar exactly once")


@pytest.mark.parametrize("name", [Path(n).stem for n in HUMAN_DECISION_AGENTS])
def test_authoring_agent_renders_human_decision_partial(
    name: str, tmp_path: Path,
) -> None:
    body = load_workflow(
        bundled_workflow_dir("imp")).agents[name].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    rendered = render_prompt(
        body,
        build_context(
            quest_title="t", event_log=log, quest_id="abc",
            quest_home=".imp/quests/abc",
            cur_base_head="X", orig_base_head="X",
            vars={"fast_path": False, "plan_index": 1, "plan_total": 1,
                  "task_total": 1, "integration_fork": "P"},
            project_facts={}),
        workflow_dir=bundled_workflow_dir("imp"))
    # Sentinel lives entirely on one partial line (the canonical sentence wraps
    # across a newline, so assert a within-line substring that survives the wrap).
    assert "never the bar for code quality." in rendered, (
        f"{name}: the human-decision include did not resolve into the prompt")


def test_no_agent_carries_standalone_observable_behavior_trigger() -> None:
    for md in AGENTS_DIR.glob("*.md"):
        assert "observable/external behavior" not in md.read_text(), (
            f"{md.name}: still carries the removed standalone "
            "'observable/external behavior' escalation trigger")


def test_no_prompt_references_transport_non_interactive() -> None:
    roots = [Path("src/imp/assets/workflows/imp")]
    offenders = [str(p) for root in roots for p in root.rglob("*.md")
                 if "run.non_interactive" in p.read_text()]
    assert offenders == [], offenders


def test_task_lead_reads_subagent_final_message_not_named_field() -> None:
    text = TASK_LEAD.read_text()
    # The coder's result/report are read from its prose final message, not a
    # `result:` / `report` named output field (the subagent has no schema).
    assert "`result: NEEDS_CONTEXT`" not in text
    assert "subagent's\n`report` surfaced" not in text
    assert "final message reports `NEEDS_CONTEXT` or `BLOCKED`" in text


def test_deferred_sweep_reads_subagent_final_message_not_named_field() -> None:
    text = SWEEP.read_text()
    assert "When a subagent returns\n`result: NEEDS_CONTEXT`" not in text
    assert "final message reports\n`NEEDS_CONTEXT` or `BLOCKED`" in text


@pytest.mark.parametrize("tree", ["imp", "patent"])
def test_dispatched_subagents_have_no_schema_or_complete_step(tree: str) -> None:
    wf = load_workflow(bundled_workflow_dir(tree))
    dispatchable: set[str] = set()
    for a in wf.agents.values():
        dispatchable.update(a.subagents)
    assert dispatchable, f"{tree}: expected at least one dispatched subagent"
    for name in sorted(dispatchable):
        sub = wf.agents[name]
        assert sub.output_schema.fields == (), (
            f"{tree}:{name} still declares an output schema — a dispatched "
            "subagent reports in prose")
        assert "complete_step" not in sub.prompt_template_source, (
            f"{tree}:{name} still instructs complete_step — it has no such tool")


def test_run_namespace_no_longer_exposes_non_interactive(tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    ctx = build_context(quest_title="t", event_log=log)
    with pytest.raises(PromptRenderError):
        render_prompt("{{ run.non_interactive }}", ctx)


def test_finalize_auto_resolves_needs_decision_only_under_autonomy(
        tmp_path: Path) -> None:
    log = JsonlEventLog(tmp_path / "events.jsonl")
    for tree in ("imp",):
        wdir = bundled_workflow_dir(tree)
        body = (wdir / "agents" / "finalize.md").read_text()
        assert "run.non_interactive" not in body   # transport flag never leaks

        def render(autonomous: bool, body: str = body, wdir: Path = wdir) -> str:
            return render_prompt(body, build_context(
                quest_title="t", event_log=log, quest_id="abc",
                quest_home=".imp/quests/abc",
                project_facts={}, autonomous=autonomous,
                vars={"auto_merge": True}),
                workflow_dir=wdir).lower()

        attended = render(False)
        autonomous = render(True)

        # Attended: a human walks the needs-decision items at the gate.
        assert "needs-decision" in attended
        assert "no human at the gate" not in attended

        # Autonomous: no human walk — every needs-decision item is seeded to the
        # backlog and the gate takes the merge disposition itself; only an
        # unrecoverable problem still pauses for a human.
        assert "no human at the gate" in autonomous
        assert "seeded <new-change-id>" in autonomous
        assert "pauses** for a human even under" in autonomous


def test_finalize_cover_check_two_stage_funnel() -> None:
    text = FINALIZE.read_text()
    low = text.lower()
    # A dedicated cover-check runs before §2 decides the gate.
    assert "cover-check" in low
    # Stage 1: candidate finder — imp search -l over active + backlog, excluding self.
    assert "imp search --active --backlog -l" in text
    assert "candidate finder" in low
    assert "{{ change.id }}" in text
    # Stage 2: confirm intent by reading the candidate's stated purpose.
    assert "imp show <id>" in text
    assert "imp show <id> spec" in text
    assert "incidental mention" in low
    # Only verify-family tolerations are cover-eligible; design/spec ones never are.
    assert "verify-family toleration" in low
    assert "never** cover-checked" in low
    assert "spec-finalizer" in low
    assert "plan-writer" in low


def test_finalize_cover_check_fails_safe_to_surface() -> None:
    text = FINALIZE.read_text()
    low = text.lower()
    assert "surface, never suppress" in low
    assert "positively confirmed" in low
    # A confirmed cover rewrites the line to the covered-by terminal marker,
    # drops it from open_items, and notes the auto-cover.
    assert "— covered-by <id>" in text
    assert "drop it from `open_items`" in text
    assert "note the auto-cover" in low


def test_finalize_seed_paths_embed_test_identity() -> None:
    text = FINALIZE.read_text()
    low = text.lower()
    # Both the autonomous seed and the interactive defer embed the test identity
    # in the new change's create_change description.
    assert low.count("must embed the failing test's identity") >= 2
    assert "node id / name / signature" in low


def test_finalize_covered_by_is_terminal_marker() -> None:
    text = FINALIZE.read_text()
    low = text.lower()
    # The terminal-marker note names covered-by alongside the existing gate markers.
    assert "`covered-by <id>` are all **terminal**" in text
    assert "seeded <id>" in low
    assert "ignored-at-gate" in low
