from __future__ import annotations

import re
from pathlib import Path

import pytest

from imp.agents.prompts import build_context, render_prompt
from imp.assets import bundled_workflow_dir
from imp.engine.loader import load_workflow
from imp.engine.state import JsonlEventLog

AGENTS_DIR = Path("src/imp/assets/workflows/imp/agents")

# Subagents (non-interactive, dispatched via `task`): the contract under test.
SUBAGENT_FILES = ["coder.md", "code-reviewer.md"]
# Interactive STEPS: the executor wires their resume hooks, so a turn-ending
# question is correct for them. Exempt from the no-bare-question rule.
INTERACTIVE_STEP_FILES = ["front-door.md", "spec-finalizer.md", "finalize.md"]

SKILLS_DIR = Path("src/imp/assets/workflows/imp/skills")

# Falsifiability-hardening guards: each component's edit leaves a marker in a
# specific file. These are per-(path, marker) pairs spanning skills/ and direct
# agents/ reads (including code-reviewer, already in SUBAGENT_FILES for the
# complete_step guards — reused here for a *different* marker, not re-listed for
# the same purpose). NOT a uniform sweep over SUBAGENT_FILES. A presence guard
# proves the marker exists somewhere in the file; section placement is left to
# author self-review and the reviewer pass.
FALSIFIABILITY_MARKERS = [
    (AGENTS_DIR / "plan-code-reviewer.md", "falsifiab"),                  # C: whole-change backstop
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


def test_front_door_infers_intent_from_pasted_context(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    assert "context instead of intent" in out
    assert "wait_for_user" in out
    assert "infer" in out.lower()


TURN_TERMINAL_REVIEWERS = [
    "plan-code-reviewer.md", "code-reviewer.md", "plan-reviewer.md",
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
        assert re.search(rf"\b{re.escape(stale)}\b", low) is None, (
            f"reintroduced stale finalize vocabulary: {stale!r}")


PLAN_CYCLE_CHECK = AGENTS_DIR / "plan-cycle-check.md"


def test_plan_cycle_check_emits_no_task_counter() -> None:
    low = PLAN_CYCLE_CHECK.read_text().lower()
    # The accumulated-task review counter is gone: per-plan review needs no count.
    assert "running_tasks" not in low
    assert "tasks_since_review" not in low
    assert "review_batch_size" not in low



def test_plan_cycle_check_owns_coverage_no_plan_gap_reopen() -> None:
    text = PLAN_CYCLE_CHECK.read_text()
    # The review can no longer re-open the cycle via plan_gap; that sentence is gone.
    assert "plan_gap" not in text
    assert "re-open the cycle" not in text
    # It is the SOLE completeness guard now.
    assert "sole" in text.lower() and "completeness" in text.lower()


def test_plan_cycle_check_consumes_deviation_notes() -> None:
    text = PLAN_CYCLE_CHECK.read_text()
    low = text.lower()
    # Both forward note types are recognized.
    assert "needs-replan" in low
    assert "resolved-forward" in low
    # An open needs-replan checkbox triggers more_plans; a consumed one does not.
    assert "[ ]" in text and "[x]" in text
    # A needs-decision item suppresses the backstop for its scope.
    assert "needs-decision" in low and "suppress" in low
    # The backstop overrides only upward.
    assert "upward" in low


CODE_REVIEWER = AGENTS_DIR / "plan-code-reviewer.md"


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
    assert "never** call `wait_for_user`" in low
    # It always returns a green tree, reverting forward through the coder.
    assert "revert it forward" in low
    assert "green tree" in low
    # The notes live in the durable region that survives the end-of-plan collapse.
    assert "survives the end-of-plan collapse" in low
    # The TRANSIENT per-task narrative (dropped at collapse) must carry NO durable
    # note: step 5 records commit subjects so buried tasks can be re-found for a fixup,
    # and per-task entries are kept only while the plan is in flight.
    assert "re-found for a fixup" in low
    assert "only while" in low


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
    assert "tolerated-failures.md" not in text       # the separate record is retired
    assert "DEFERRED verify:" in text                # the gate-sourced toleration line
    assert "## Deferred Followups" in text           # recorded in the followups list
    assert "— needs-decision:" in text          # born in needs-decision state (em dash)
    assert "verify_tolerated" in text                # the signal it emits
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
    # No separate tolerated-failures record or section — tolerations are ordinary
    # needs-decision items now, surfaced through the existing walk.
    assert "tolerated-failures.md" not in text
    assert "Surface tolerated verification failures" not in text
    assert "<signature>: <justification>" not in text
    # They reach finalize as DEFERRED verify:/sweep-verify needs-decision lines.
    assert "DEFERRED verify:" in text
    assert "sweep-verify" in text
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
    assert "plan-code-reviewer append it" in text
    assert "review-panel" not in text


def test_deferred_sweep_documents_verify_triage() -> None:
    text = SWEEP.read_text()
    low = text.lower()
    # The toleration is a Deferred Followups line sourced `sweep-verify`; the
    # separate record is retired.
    assert "tolerated-failures.md" not in text
    assert "DEFERRED sweep-verify:" in text
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
    "spec-reviewer.md", "plan-reviewer.md", "plan-code-reviewer.md",
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


def test_spec_writer_recognizes_front_door_handoff() -> None:
    body = _read("spec-finalizer.md")
    # The handoff opening and its adopt-with-light-review section are named.
    assert "Adopt the spec drafted with the user and finalize it:" in body
    assert "Front-door handoff" in body
    # head_changed is re-derived from the two persisted heads, not the tool return.
    assert "change.cur_base_head" in body
    assert "change.orig_base_head" in body


NO_FIXUP_RULE_AGENTS = ["task-lead.md", "deferred-sweep.md", "reconciler.md"]


@pytest.mark.parametrize("name", NO_FIXUP_RULE_AGENTS)
def test_committing_agents_carry_no_fixup_outside_change_rule(name: str) -> None:
    # The standing rule: never --fixup/--squash a commit the change does not own; a
    # base-traced failure is fixed forward; a bounced surviving directive becomes a
    # standalone or new forward commit.
    low = _read(name).lower()
    assert "does not own" in low
    assert "surviving directive" in low
    assert "forward" in low


def test_spec_writer_handoff_renders_drift_branch_on_moved_base(tmp_path) -> None:
    body = _read("spec-finalizer.md")
    log = JsonlEventLog(tmp_path / "events.jsonl")
    moved = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc",
        cur_base_head="NEW", orig_base_head="OLD",
        project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp")).lower()
    # Key on a phrase unique to the handoff true-branch.
    assert "the base branch advanced since the draft started" in moved   # head_changed=true
    assert "adopt near-silently" not in moved

    same = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc",
        cur_base_head="SAME", orig_base_head="SAME",
        project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp")).lower()
    assert "adopt near-silently" in same           # head_changed=false branch
    assert "the base branch advanced since the draft started" not in same

    headless_moved = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc",
        cur_base_head="NEW", orig_base_head="OLD",
        non_interactive=True,
        project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp")).lower()
    assert "the base branch advanced since the draft started" in headless_moved
    assert "do **not** call `wait_for_user`" in headless_moved


def test_front_door_documents_split_off_a_change(tmp_path) -> None:
    body = load_workflow(bundled_workflow_dir("imp")).agents[
        "front-door"].prompt_template_source
    log = JsonlEventLog(tmp_path / "events.jsonl")
    out = render_prompt(body, build_context(
        change_title="t", event_log=log, change_id="abc",
        change_home=".imp/changes/abc", project_facts={}, guidance=""),
        workflow_dir=bundled_workflow_dir("imp"))
    # The capability is documented (create_change is otherwise absent from this file).
    assert "create_change" in out
    # Confirm-first discipline: propose, then gate before spawning.
    assert "before calling `create_change`" in out
    # depends_on is conditional and the current change id is wired into the example.
    assert 'depends_on=["abc"]' in out
    # Plain-language resume hint for the spun-off change.
    assert "imp resume" in out
