from __future__ import annotations

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
    "spec-reviewer.md", "completeness-slice.md",
    "boundaries-slice.md", "spec-compliance-slice.md", "plan-cycle-check.md",
    "review-panel.md",
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
    # Qualifying nits ride the same fixup round, folded by autosquash (AC 4).
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


def test_finalize_surfaces_only_needs_decision_and_rolls_up_fixes() -> None:
    low = FINALIZE.read_text().lower()
    # built carries a one-line rollup counting the opportunistic fixes.
    assert "opportunistic improvement" in low


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
    # The three per-item dispositions and their terminal markers.
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
