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


def test_spec_reviewer_briefed_on_decision_markers() -> None:
    text = (AGENTS / "spec-reviewer.md").read_text()
    assert "*(decided during review)*" in text
    assert "*(user-confirmed during review)*" in text
    assert "expected" in text.lower()
    # must not be flagged as a placeholder/meta-commentary defect
    assert "meta-commentary" in text.lower()


def _plan_writer() -> str:
    return (AGENTS / "plan-writer.md").read_text()


def test_plan_writer_has_ask_vs_decide_and_decisions_section() -> None:
    text = _plan_writer()
    assert "*(decided during planning)*" in text
    assert "*(user-confirmed during planning)*" in text
    assert "## Decisions" in text
    assert "wait_for_user" in text
    # minors pass ban kept (the phrase wraps across a line break in the file;
    # "do not reopen planning" is distinctive and sits on one line)
    assert "do not reopen planning" in text


def test_plan_writer_recognition_taxonomy_full_strings() -> None:
    text = _plan_writer()
    for s in (
        "Here's review feedback to address:",
        "No blocking issues — integrate these minor improvements; a final review follows:",
        "The plan didn't match the codebase. Here's what the implementer found:",
        "More plans are needed for this spec. Coverage assessment:",
        "The review panel found the plan needs revision. Findings:",
        "The code reviewer found the plan needs revision. Findings:",
        "Your previous attempt completed but was not accepted",
        "previous step output recorded; superseded by the feedback below",
    ):
        assert s in text, f"plan-writer taxonomy missing: {s!r}"
