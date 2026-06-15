"""Schema + prompt-content + yaml->prompt parity guards for the confirm gate."""
from __future__ import annotations

from pathlib import Path

from imp.agents.transcript import RESUME_SUPERSEDED_NOTE
from imp.assets import default_workflow_dir
from imp.engine.feedback import _WRAPPER
from imp.engine.loader import load_workflow
from imp.engine.types import Goto

AGENTS = Path("src/imp/assets/workflows/default/agents")


def test_spec_writer_schema_drops_unseen_changes() -> None:
    wf = load_workflow(default_workflow_dir())
    schema = wf.agents["spec-writer"].output_schema
    phase = next(f for f in schema.fields if f.name == "phase")
    assert phase.type == "enum"
    assert set(phase.values) == {"written"}
    assert all(f.name != "unseen_changes" for f in schema.fields)


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
    # the escalation channel is named in the Ask-vs-decide policy itself
    # (a bare "wait_for_user" also appears in frontmatter, so anchor on the
    # policy phrasing to actually guard this section).
    assert "Escalate via `wait_for_user`" in text
    # the decide-and-record default phrasing is present
    assert "decide and record" in text.lower()


def test_spec_writer_uses_artifact_commit_not_amend() -> None:
    text = _spec_writer()
    assert "imp artifact-commit spec" in text
    assert "--fixup" not in text
    assert "git commit --amend" not in text


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


def test_plan_reviewer_briefed_on_decisions_section() -> None:
    text = (AGENTS / "plan-reviewer.md").read_text()
    assert "## Decisions" in text or "Decisions section" in text
    assert "during planning" in text
    assert "expected" in text.lower()


def test_plan_writer_recognition_taxonomy_full_strings() -> None:
    text = _plan_writer()
    for s in (
        "Here's review feedback to address:",
        "No blocking issues — integrate these minor improvements; a final review follows:",
        "The plan didn't match the codebase. Here's what the implementer found:",
        "More plans are needed for this spec. Coverage assessment:",
        "The review found the plan needs revision. Findings:",
        "The integration pass found the plan needs revision. Findings:",
        "Your previous attempt completed but was not accepted",
        "previous step output recorded; superseded by the feedback below",
    ):
        assert s in text, f"plan-writer taxonomy missing: {s!r}"


_WRITER_FILES = {
    "spec-writer": AGENTS / "spec-writer.md",
    "plan-writer": AGENTS / "plan-writer.md",
}


def _writer_targeting_goto_rules():
    wf = load_workflow(default_workflow_dir())
    for step in wf.flat_steps():
        for rule in step.rules:
            act = rule.action
            if isinstance(act, Goto) and act.target in _WRITER_FILES:
                yield step, rule, act


def test_every_writer_feedback_string_appears_in_target_prompt() -> None:
    # yaml is the source of truth: every feedback string on a writer-targeting
    # rule must appear verbatim (plain substring) in the target prompt file.
    for step, _rule, act in _writer_targeting_goto_rules():
        if act.feedback:
            prompt = _WRITER_FILES[act.target].read_text()
            assert act.feedback in prompt, (
                f"{step.name}->{act.target}: feedback string absent from prompt:\n"
                f"  {act.feedback!r}")


def test_every_writer_targeting_resume_carries_feedback_or_is_script_gate() -> None:
    # A feedback-less resume would arrive with a different synthesized tool_result
    # ("…resuming") than the prompts document — except script gates, where the
    # engine supplies the _WRAPPER feedback instead of a rule feedback.
    for step, _rule, act in _writer_targeting_goto_rules():
        if act.resume and not act.feedback:
            assert step.kind == "script", (
                f"{step.name}->{act.target}: non-script resume with no feedback")


def test_engine_sourced_taxonomy_strings_present_in_both_prompts() -> None:
    wrapper_prefix = _WRAPPER.split("{detail}")[0].strip()
    for path in _WRITER_FILES.values():
        text = path.read_text()
        assert wrapper_prefix in text, f"{path.name}: missing _WRAPPER prefix"
        assert RESUME_SUPERSEDED_NOTE in text, (
            f"{path.name}: missing synthesized tool_result note")


def test_spec_gate_and_confirm_vars_are_gone() -> None:
    wf = load_workflow(default_workflow_dir())
    names = {v.name for v in wf.vars.decls}
    assert "spec_gate_pending" not in names
    assert "spec_gate_passed" not in names
    assert "spec_confirm" not in names
    assert "spec_unseen_changes" not in names


def test_spec_phase_steps_have_no_confirm_or_exit_gate() -> None:
    wf = load_workflow(default_workflow_dir())
    spec = next(p for p in wf.phases if p.name == "spec")
    names = [s.name for s in spec.steps]
    assert "spec-exit-gate" not in names
    assert "spec-confirm" not in names
    assert names == ["spec-writer", "spec-verify-commit", "spec-reviewer"]



