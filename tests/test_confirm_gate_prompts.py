"""Schema + prompt-content + yaml->prompt parity guards for the confirm gate."""
from __future__ import annotations

from pathlib import Path

from imp.assets import default_workflow_dir
from imp.engine.loader import load_workflow

AGENTS = Path("src/imp/assets/workflows/default/agents")


def test_spec_writer_schema_gains_confirmed_phase_and_unseen_changes() -> None:
    wf = load_workflow(default_workflow_dir())
    schema = wf.agents["spec-writer"].output_schema
    phase = next(f for f in schema.fields if f.name == "phase")
    assert phase.type == "enum"
    assert set(phase.values) == {"written", "confirmed"}
    unseen = next(f for f in schema.fields if f.name == "unseen_changes")
    assert unseen.type == "bool"
    assert unseen.required is True


def _spec_writer() -> str:
    return (AGENTS / "spec-writer.md").read_text()


def test_spec_writer_drops_rework_solicitation_bans() -> None:
    text = _spec_writer()
    # The standalone rework-close ban is gone (replaced by the policy pointer).
    assert "Never re-solicit the user on rework" not in text
    # …but the kept bans remain.
    assert "do not re-solicit the user" in text          # pre-commit self-review
    assert "do\n**not** ask the user anything" in text or \
           "do **not** ask the user anything" in text     # minors pass


def test_spec_writer_carries_ask_vs_decide_policy_and_markers() -> None:
    text = _spec_writer()
    assert "*(decided during review)*" in text
    assert "*(user-confirmed during review)*" in text
    # the escalation channel is named
    assert "wait_for_user" in text
    # the decide-and-record default phrasing is present
    assert "decide and record" in text.lower()


def test_spec_writer_has_confirm_and_finalize_protocol() -> None:
    text = _spec_writer()
    assert "## Confirm and finalize" in text
    # silent-confirm fast exit is spelled out
    assert "phase=\"confirmed\"" in text or 'phase="confirmed"' in text
    # the only exit-to-planning contract: confirmed carries no uncommitted edits
    assert "must never carry uncommitted edits" in text
    # edits at the gate flow back through review as written + unseen_changes:false
    assert "unseen_changes=false" in text.lower().replace(" ", "") or \
           "unseen_changes: false" in text


def test_spec_writer_has_unseen_changes_emit_section() -> None:
    text = _spec_writer()
    assert "emit `unseen_changes` on every" in text
    # the per-shape definition (the field's meaning) is documented
    assert "without the user" in text and "direction" in text


def test_spec_writer_has_amend_precondition_with_fixup_fallback() -> None:
    text = _spec_writer()
    assert "docs: add spec for <slug>" in text   # HEAD-is-spec-commit subject
    assert "--fixup=\":/docs: add spec for <slug>\"" in text


def test_spec_writer_recognition_is_preamble_primary() -> None:
    text = (AGENTS / "spec-writer.md").read_text()
    # The reviewer-rework recognition no longer keys on the wrapper text.
    # The wrapper phrase survives ONLY on the script-gate row, so it appears
    # at most as a script-gate cue — assert the *old* mis-keyed sentence is gone.
    assert ("a message beginning \"Your previous attempt completed but was not "
            "accepted. The spec-reviewer reviewed your work") not in text
    # This task rewrites the line-109 recognition paragraph, so its inline
    # solicitation ban is gone (the policy now lives in "Ask vs. decide").
    assert "Do NOT ask the user anything" not in text
    # The taxonomy carries the full, untruncated feedback strings verbatim.
    assert "Here's review feedback to address:" in text
    assert ("No blocking issues — integrate these minor improvements; a final "
            "review follows:") in text
    assert ("The review cycle is complete and the spec is accepted. Surface any "
            "significant decisions made during review to the user and confirm "
            "before finalizing:") in text
    # The synthesized tool_result note (resume-vs-goto discriminator) is present.
    assert "previous step output recorded; superseded by the feedback below" in text
