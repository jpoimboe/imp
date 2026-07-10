from __future__ import annotations

from pathlib import Path

PARTIALS = Path("src/imp/assets/workflows/imp/partials")


def test_spec_design_principles_carries_judgment_lens_only() -> None:
    text = (PARTIALS / "spec-design-principles.md").read_text()
    # design-for-clarity
    assert "do not propose unrelated refactoring" in text
    # cover-the-design (the concrete coverage list)
    assert "architecture, components, data flow, error handling, and testing" in text
    # YAGNI
    assert "YAGNI ruthlessly" in text
    # It must NOT carry dialogue-conduct prose (that lives in brainstorm-dialogue).
    assert "One question at a time" not in text
    assert "Propose 2" not in text
    assert "build this out now" not in text.lower()
    # No superpowers attribution line.
    assert "superpowers" not in text.lower()


def test_brainstorm_dialogue_carries_conduct_only() -> None:
    text = (PARTIALS / "brainstorm-dialogue.md").read_text()
    # opening-message taxonomy is now a direct run.opening conditional, not a
    # token-inspection of the literal <imp:begin/> opener.
    assert "<imp:begin/>" not in text
    assert "{% if run.opening %}" in text
    assert "names a spec file" in text
    # one question at a time — relocated into the shared live-gate partial
    assert "One question at a time" not in text
    assert "One question at a time" in (PARTIALS / "live-gate-dialogue.md").read_text()
    # the yielding paragraph was relocated into the partial as well
    assert "Yielding to the human" not in text
    # the dialogue partial now pulls in the shared live-gate posture
    assert '{% include "partials/live-gate-dialogue.md" %}' in text
    # research before you converge — the explorer dispatch idiom moved into the
    # shared delegate-exploration partial (included here), so assert the include.
    assert "Research before you converge" in text
    assert '{% include "partials/delegate-exploration.md" %}' in text
    assert "no cost gating" in text
    # propose 2-3 + design-then-approve in sections
    assert "present it in sections" in text
    # build-or-save gate
    assert "build this out now" in text.lower()
    assert "imp resume <id>" in text
    # final readback
    assert "Final readback before committing" in text
    assert "consolidated" in text.lower()
    # No superpowers attribution line.
    assert "superpowers" not in text.lower()


def test_brainstorm_dialogue_omits_finalizer_only_sections() -> None:
    text = (PARTIALS / "brainstorm-dialogue.md").read_text()
    # These belong to spec-finalizer, never the dialogue partial.
    assert "Threshold handoff" not in text
    assert "Rework Resumption" not in text
    # Completion plumbing reconciled to threshold's ceremony model: no spec-writer
    # phase output and no downstream spec-reviewer reference survive the move.
    assert "phase: written" not in text
    assert "spec-reviewer" not in text
    assert "spec-writer" not in text


def test_brainstorm_dialogue_bare_open_does_not_freeze_concierge_or_scout() -> None:
    text = (PARTIALS / "brainstorm-dialogue.md").read_text()
    low = text.lower()
    # The spec-writer build-only freeze must NOT survive the move into threshold.
    assert "do not read any files, do not explore the codebase" not in low
    assert "what the user wants to build" not in low
    # The bare open is reframed as threshold's open, three-hat invitation.
    assert "take stock of existing work" in low
    assert "their reply picks the hat" in low


def test_spec_design_principles_carries_illegal_states_principle() -> None:
    text = (PARTIALS / "spec-design-principles.md").read_text()
    assert "**Make illegal states unrepresentable.**" in text
    assert "rules out the bad case over one that admits it and then validates or" in text
    assert "Push correctness into the shape of the data, not into runtime guards." in text


def test_live_gate_dialogue_carries_door_agnostic_posture_only() -> None:
    text = (PARTIALS / "live-gate-dialogue.md").read_text()
    # Door-agnostic posture present.
    assert "Converse until ready" in text
    assert "recommendation-anchored" in text
    assert "no co-equal dangling alternative" in text
    assert "One question at a time" in text
    assert "closing summary" in text
    assert "wait_for_user" not in text
    assert "bare turn-end **is** the yield" in text
    assert "End your turn with your question to yield" in text
    # The two finalize-only postures must NOT live in the shared partial.
    assert "Clarifying questions are first-class" not in text
    assert "Engage a concern straight" not in text
    assert "within normal noise" not in text
    # No superpowers attribution line.
    assert "superpowers" not in text.lower()


def test_brainstorm_dialogue_carries_enumeration_and_grill_mode() -> None:
    text = (PARTIALS / "brainstorm-dialogue.md").read_text()
    # (a) enumerate-before-commit for user-visible outcomes
    assert "Enumerate before committing" in text
    assert "unenumerated deferral" in text
    # (b) whole-surface after-state inventory with the real-ANSI mockup constraint
    assert "Walk the whole surface" in text
    assert "after-state inventory" in text
    assert "real ANSI SGR colors" in text
    assert "never depict a color the codes cannot produce" in text
    # (c) confirmed values are written into the spec verbatim
    assert "Enumerated values are spec content" in text
    assert "into the spec verbatim" in text
    # (d) on-request grill mode; prose-batch pacing stated as the default
    assert "Grill mode on request" in text
    assert "Prose-batch pacing is the default" in text
    assert "one-question-at-a-time" in text
    assert "recommended answer" in text
    assert "looked up, never asked" in text
    # The grill copy must not collide with the case-exact ban other tests pin.
    assert "One question at a time" not in text


def test_brainstorm_dialogue_renders_seeded_vs_unseeded_branch() -> None:
    from imp.agents.prompts import build_context, render_prompt
    from imp.engine.state import InMemoryEventLog
    src = (PARTIALS / "brainstorm-dialogue.md").read_text()
    wf = PARTIALS.parent  # workflow_dir so {% include %} resolves

    seeded = render_prompt(src, build_context(
        change_title="t", event_log=InMemoryEventLog(),
        opening="add a dark-mode toggle"), workflow_dir=wf)
    assert "add a dark-mode toggle" in seeded          # intent rendered inline
    assert "Open by asking" not in seeded              # not the bare-open branch

    unseeded = render_prompt(src, build_context(
        change_title="t", event_log=InMemoryEventLog()), workflow_dir=wf)
    assert "add a dark-mode toggle" not in unseeded
    assert "Open by asking" in unseeded                # bare-open elicit branch

