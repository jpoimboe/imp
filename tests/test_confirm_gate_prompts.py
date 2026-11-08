"""Schema + prompt-content + yaml->prompt parity guards for the confirm gate."""
from __future__ import annotations

from pathlib import Path

from imp.agents.transcript import REWORK_RESULT_FRAME
from imp.assets import bundled_workflow_dir
from imp.engine.feedback import _WRAPPER
from imp.engine.loader import load_workflow
from imp.engine.types import Goto

AGENTS = Path("src/imp/assets/workflows/default/agents")


def test_spec_writer_schema_drops_unseen_changes() -> None:
    wf = load_workflow(bundled_workflow_dir("default"))
    schema = wf.agents["spec-finalizer"].output_schema
    phase = next(f for f in schema.fields if f.name == "phase")
    assert phase.type == "enum"
    assert set(phase.values) == {"written"}
    assert all(f.name != "unseen_changes" for f in schema.fields)


def _spec_writer() -> str:
    return (AGENTS / "spec-finalizer.md").read_text()


def test_spec_writer_drops_rework_solicitation_bans() -> None:
    text = _spec_writer()
    # The standalone rework-close ban is gone (replaced by the policy pointer).
    assert "Never re-solicit the user on rework" not in text
    # …but the kept ban remains: the pre-commit self-review still fixes inline.
    assert "do not re-solicit the user" in text


def test_spec_writer_carries_ask_vs_decide_policy_and_markers() -> None:
    text = _spec_writer()
    assert "*(decided during review)*" in text
    assert "*(user-confirmed during review)*" in text
    # the escalation channel is named in the Ask-vs-decide policy itself.
    assert "Escalate by ending your turn with the question" in text
    assert "wait_for_user" not in text
    # the decide-and-record default phrasing is present
    assert "decide and record" in text.lower()


def test_spec_writer_does_not_invoke_artifact_commit() -> None:
    text = _spec_writer()
    assert "imp artifact-commit" not in text
    assert "--fixup" not in text
    assert "git commit --amend" not in text


def test_spec_writer_recognition_is_preamble_primary() -> None:
    text = (AGENTS / "spec-finalizer.md").read_text()
    # The reviewer-rework recognition no longer keys on the wrapper text.
    # The wrapper phrase survives ONLY on the script-gate row, so it appears
    # at most as a script-gate cue — assert the *old* mis-keyed sentence is gone.
    assert ("a message beginning \"Your previous attempt completed but was not "
            "accepted. The spec-reviewer reviewed your work") not in text
    # This task rewrites the line-109 recognition paragraph, so its inline
    # solicitation ban is gone (the policy now lives in "Ask vs. decide").
    assert "Do NOT ask the user anything" not in text
    # The taxonomy carries the reviewer-rework feedback string verbatim.
    assert "Here's review feedback to address:" in text
    # The synthesized is_error rework-result frame (resume-vs-goto discriminator) is present.
    assert REWORK_RESULT_FRAME in text


def _flat(text: str) -> str:
    return " ".join(text.split())


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


_FINALIZER_DECISIONS_SECTION = """## Decisions reached with the user

A decision the brainstorm settled with the user binds the spec on every turn, first write and rework
alike. Check it against `imp log {{ quest.id }} --step threshold`.

- Dropped or drifted by mistake → restore it.
- New information shows it is wrong → make the better call, record the rationale in the Decisions
  summary, and append one open item for the user to ratify at the merge gate (reader form under
  **Surfacing a decision you cannot resolve**):
  `- [ ] DEFERRED spec-finalizer: <decision made with the user> → <what the spec does instead>
  [scope: quest] — needs-decision: <the new information>`

Never yield for it. A departure with no open item is a defect the reviewer blocks."""

_FINALIZER_ESCALATION = (
    "**Escalate by ending your turn with the question** only when the decision is "
    "genuinely the user's to make: the finding exposes a real fork in *user intent* "
    "— plausible readings with materially different outcomes that nothing in the "
    "brainstorm, spec, or codebase settles. A departure from a decision already "
    "reached with the user never qualifies; it follows **Decisions reached with "
    "the user**."
)


def test_spec_finalizer_verifies_decision_claims_against_dialogue() -> None:
    rework = _flat(_section(_spec_writer(), "## Rework Resumption"))
    assert (
        "use `read_file`/`grep` to check any code claim, and "
        "`imp log {{ quest.id }} --step threshold` (the brainstorm dialogue) to "
        "check a claim about what was decided."
    ) in rework


def test_spec_finalizer_routes_user_decision_departures_to_merge_gate() -> None:
    text = _spec_writer()
    assert "## Decisions reached with the user" in text
    assert text.index("## Decisions reached with the user") < text.index(
        "## Ask vs. decide")
    assert _flat(_FINALIZER_DECISIONS_SECTION) in _flat(text)
    # the deferred line form sits on one line so an agent can copy it whole
    assert (
        "`- [ ] DEFERRED spec-finalizer: <decision made with the user> → <what the "
        "spec does instead> [scope: quest] — needs-decision: <the new information>`"
    ) in text


def test_spec_finalizer_escalates_only_on_a_user_intent_fork() -> None:
    text = _spec_writer()
    assert "user-stated requirement" not in text
    ask = _section(text, "## Ask vs. decide")
    assert "conflicting directives" not in ask
    assert _FINALIZER_ESCALATION in _flat(ask)
    for kept in ("*(decided during review)*", "*(user-confirmed during review)*",
                 "Escalate by ending your turn with the question",
                 "decide and record"):
        assert kept in ask, kept


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
    assert "yield to the human" in text
    assert "wait_for_user" not in text


def test_plan_reviewer_briefed_on_decisions_section() -> None:
    text = (AGENTS / "plan-reviewer.md").read_text()
    assert "## Decisions" in text or "Decisions section" in text
    assert "during planning" in text
    assert "expected" in text.lower()


def test_plan_writer_recognition_taxonomy_full_strings() -> None:
    text = _plan_writer()
    for s in (
        "Here's review feedback to address:",
        "More plans are needed for this spec. Coverage assessment:",
        "The review found the plan needs revision. Findings:",
        "The integration pass found the plan needs revision. Findings:",
        "Your previous attempt completed but was not accepted",
        REWORK_RESULT_FRAME,
    ):
        assert s in text, f"plan-writer taxonomy missing: {s!r}"


def test_plan_writer_documents_the_spec_approval_goto_as_planning_work() -> None:
    # A clean spec review now gotos plan-writer instead of bouncing the finalizer,
    # so the taxonomy must name the shape AND frame it as planning, not a rework.
    text = _plan_writer()
    assert "| Spec approved | goto (fresh session) |" in text
    row = next(ln for ln in text.splitlines()
               if ln.startswith("| Spec approved |"))
    assert "The spec is approved — no blocking issues." in row
    assert "Planning work, not a rework" in row


_WRITER_FILES = {
    "spec-finalizer": AGENTS / "spec-finalizer.md",
    "plan-writer": AGENTS / "plan-writer.md",
}


def _writer_targeting_goto_rules():
    wf = load_workflow(bundled_workflow_dir("default"))
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
        assert REWORK_RESULT_FRAME in text, (
            f"{path.name}: missing synthesized rework-result frame")


def test_spec_gate_and_confirm_vars_are_gone() -> None:
    wf = load_workflow(bundled_workflow_dir("default"))
    names = {v.name for v in wf.vars.decls}
    assert "spec_gate_pending" not in names
    assert "spec_gate_passed" not in names
    assert "spec_confirm" not in names
    assert "spec_unseen_changes" not in names


def test_spec_phase_steps_have_no_confirm_or_exit_gate() -> None:
    wf = load_workflow(bundled_workflow_dir("default"))
    spec = next(p for p in wf.phases if p.name == "spec")
    names = [s.name for s in spec.steps]
    assert "spec-exit-gate" not in names
    assert "spec-confirm" not in names
    assert names == ["spec-finalizer", "spec-verify-commit", "spec-reviewer"]



