from __future__ import annotations

from pathlib import Path

import pytest

AGENTS = Path("src/imp/assets/workflows/default/agents")


def _flat(text: str) -> str:
    return " ".join(text.split())


def _agent(name: str) -> str:
    return (AGENTS / f"{name}.md").read_text()


def _section(text: str, heading: str) -> str:
    start = text.index(heading)
    end = text.find("\n## ", start + len(heading))
    return text[start:] if end == -1 else text[start:end]


def _record(source: str) -> str:
    return f"`- [x] DEFERRED {source}: <what was decided> [scope: quest] — decided: <why>`"


_COPY_RULE_START = {
    "plan-writer": "New user-facing copy —",
    "spec-finalizer": "> **Unenumerated copy is decided, not gated.**",
}


def _copy_rule(source: str) -> str:
    text = _agent(source)
    start = text.index(_COPY_RULE_START[source])
    return _flat(text[start:text.index("\n## ", start)])


@pytest.mark.parametrize("source", ["plan-writer", "spec-finalizer"])
def test_unpinned_copy_is_a_decided_record(source: str) -> None:
    rule = _copy_rule(source)
    assert "docs/README prose, UI strings, messages, or LLM prompt text" in rule
    assert "needs-decision" not in rule
    surfacing = _section(_agent(source), "## Surfacing a decision you cannot resolve")
    assert _record(source) in surfacing


def test_plan_writer_copy_rule_records_the_wording_as_decided() -> None:
    assert "record the wording as one `— decided:` line" in _copy_rule("plan-writer")


def test_spec_finalizer_still_pins_default_copy_as_spec_content() -> None:
    rule = _copy_rule("spec-finalizer")
    assert "Pin a reasonable default as spec content" in rule
    assert "it never gates the merge; the quest summary lists it" in rule


@pytest.mark.parametrize(("source", "pointer"), [
    ("plan-writer",
     "`— needs-decision:` is only for calls that clear the bar above; a call you "
     "already made and implemented takes the `— decided:` record below."),
    ("spec-finalizer",
     "`— needs-decision:` is only for calls that clear the bar above; a call you "
     "already made takes the `— decided:` record below."),
])
def test_genuine_call_text_points_already_made_calls_at_decided(
    source: str, pointer: str,
) -> None:
    surfacing = _section(_agent(source), "## Surfacing a decision you cannot resolve")
    assert pointer in _flat(surfacing)


_GENUINE_FORMS = [
    ("spec-finalizer",
     "`- [ ] DEFERRED spec-finalizer: <decision made with the user> → <what the spec "
     "does instead> [scope: quest] — needs-decision: <the new information>`"),
    ("spec-finalizer",
     "`- [ ] DEFERRED spec-finalizer: <one-line description> [scope: quest] "
     "— needs-decision: <why a human must decide>`"),
    ("plan-writer",
     "`- [ ] DEFERRED plan-writer: <one-line description> [scope: quest] "
     "— needs-decision: <why a human must decide>`"),
    ("reconciler",
     "`- [ ] DEFERRED reconciler: <one-line description> [scope: quest] "
     "— needs-decision: <why a human must decide>`"),
    ("task-lead", "recorded as a `— needs-decision:` Deferred Followup"),
    ("task-lead",
     "`- [ ] DEFERRED verify: <failure signature> [scope: quest] "
     "— needs-decision: <why this quest is not responsible>`"),
    ("integration-review",
     "`- [ ] DEFERRED integration-verify: <failure signature> [scope: quest] "
     "— needs-decision: <why this quest is not responsible>`"),
    ("deferred-sweep",
     "`- [ ] DEFERRED <source>: <desc> [scope: …] — needs-decision: <why>`"),
]


@pytest.mark.parametrize(("source", "form"), _GENUINE_FORMS)
def test_genuine_calls_keep_the_needs_decision_form(source: str, form: str) -> None:
    assert form in _agent(source)


def test_plan_writer_records_boundary_calls_as_decided() -> None:
    ask = _flat(_section(_agent("plan-writer"), "## Ask vs. decide"))
    assert "needs-decision" not in ask
    for path in (
        # spec scope/AC boundary decide-and-record
        "record it as **one** `— decided:` line in `{{ quest.home }}/deferred.md` "
        "(the form under \"Recording a call you already made\")",
        # "Everything else" sentence
        "Everything else → decide the most reasonable call, implement it, and — when "
        "it crosses a spec scope/AC boundary — record it as one `— decided:` line.",
        # plan-time user-confirmed contradiction
        "if it *contradicts* the spec, record it the same way plus one `— decided:` line",
    ):
        assert path in ask, path


def test_task_lead_resolved_forward_boundary_crossing_is_decided() -> None:
    text = _agent("task-lead")
    row = next(ln for ln in text.splitlines()
               if ln.strip().startswith("| **resolved-forward** |"))
    assert "a `— decided:` line if it crossed a spec/AC boundary" in row
    assert "needs-decision" not in row
    assert "ratif" not in row
    flat = _flat(text)
    assert ("If the fix crosses a spec scope/AC boundary, also append its "
            "`— decided:` record (shape below).") in flat
    assert "so the human ratifies it" not in flat
    assert _record("task-lead") in text
    # The resolved-forward note itself is unchanged.
    assert ("- resolved-forward: `- resolved-forward task-lead: <one-line "
            "description of what you fixed>`") in text


def test_reconciler_splits_on_the_human_decision_bar() -> None:
    text = _agent("reconciler")
    flat = _flat(text)
    assert ("`— needs-decision:` is only for a reconciliation that clears that bar "
            "— intent-level, costly to reverse, no reasonable default.") in flat
    assert ("A call you already made — settling a conflict between base's and the "
            "quest's intents with a reasonable default — takes one born-ticked line "
            "in the same section instead") in flat
    assert "A mechanical conflict resolution gets no line" in flat
    assert "it never gates the merge, and the quest summary lists it" in flat
    assert _record("reconciler") in text
    assert ("Both forms are a durable record for asynchronous human triage and "
            "integration-review's synchronous read") in flat
    assert "a human should still ratify it" not in flat
    bar = text.index('{% include "partials/human-decision.md" %}')
    assert bar < text.index(_record("reconciler"))
    assert (text.index(_record("reconciler"))
            < text.index("A mechanical conflict resolution gets no line"))


def test_plan_reviewer_accepts_a_decided_line_as_recorded() -> None:
    decisions = _flat(_section(_agent("plan-reviewer"), "## The Decisions section"))
    assert ("- **Recorded** — a matching `— decided:` line (an already-made call: "
            "listed in the quest summary, never ratified at the merge gate) or "
            "`— needs-decision:` line (a genuine human call `finalize` walks at the "
            "gate): do **not** raise Critical/Major.") in decisions
    assert ("- **Unrecorded** — a scope contradiction with neither a `— decided:` "
            "nor a `— needs-decision:` line (a sneak-by): flag it **Major**.") in decisions
    # The old "every recorded deviation is ratified at the gate" framing is gone.
    assert "ratifies it once" not in decisions
    assert "ride to the merge gate" not in decisions


def test_gap_check_treats_decided_alterations_as_settled() -> None:
    text = _agent("gap-check")
    backstop = _flat(_section(text, "## Coverage decision"))
    assert ("Scope that a `— decided:` line deliberately altered is **settled**: "
            "judge it against the decided behavior, not the spec's original wording; "
            "met as decided, it does not trigger a plan.") in backstop
    assert ("never author a `— needs-decision:` or `— decided:` line; your sole "
            "lever is `more_plans`") in backstop
    walk = _flat(_section(text, "## Walk the acceptance criteria clause by clause"))
    assert ("A clause deliberately altered by a `— decided:` line is **settled**: "
            "judge it against the decided behavior, not the spec's original wording; "
            "met as decided, it does not drive a plan.") in walk
    # Still a pure reader of the ledger.
    assert "read `deferred.md` and never write it" in walk


def test_integration_review_checks_both_reconciler_line_forms() -> None:
    flat = _flat(_agent("integration-review"))
    assert ("`{{ quest.home }}/deferred.md` carries any `## Deferred Followups` lines "
            "it flagged — its `DEFERRED reconciler:` lines, `— needs-decision:` and "
            "`— decided:` alike. Read both files and check each flagged "
            "reconciliation first") in flat


def test_deferred_sweep_never_re_evaluates_or_authors_decided() -> None:
    flat = _flat(_agent("deferred-sweep"))
    assert ("Any line carrying `— needs-decision:` (including a toleration), "
            "`— decided:`, or a terminal tag (`— fixed` / `— dismissed` / `— seeded` "
            "/ `— ignored-at-gate` / `— tracked-by`) is already triaged; leave it "
            "untouched.") in flat
    assert ("A `— decided:` record is a call a producer already made, born ticked; "
            "the sweep never authors one — its fixes close "
            "`— fixed in \"<commit-subject>\"`.") in flat


def test_summarizer_lists_decided_calls_after_the_headline() -> None:
    flat = _flat(_agent("summarizer"))
    assert ("- `{{ quest.home }}/deferred.md` — its `— decided:` lines are calls agents "
            "made without asking the user;") in flat
    assert ("- **Decided without asking you** — when `deferred.md` has any `— decided:` "
            "lines, follow the headline's blank line with a line reading exactly "
            "`Decided without asking you:`, then one `- <what was decided> — <why>` bullet "
            "per line, in plain user terms.") in flat
    assert ("A line rewritten to `— overridden-at-gate` is no longer decided; leave it "
            "out. Omit the section entirely when there are none.") in flat
    # After the headline (outside the merge commit body), before the optional diagram.
    assert (flat.index("- **Headline**")
            < flat.index("- **Decided without asking you**")
            < flat.index("- **Optional diagram**"))


def test_docs_describe_the_decided_record() -> None:
    readme = _flat(Path("README.md").read_text())
    assert ("- **Auto merge** — If there are no open issues for you to decide on, it "
            "auto-merges to your integration branch (this default is configurable); calls "
            "imp made on its own are listed in the quest summary rather than stopping the "
            "merge") in readme
    arch = _flat(Path("docs/internals/architecture.md").read_text())
    assert ("(any agent may file a `— needs-decision:` item for a human call, or a "
            "born-ticked `— decided:` record of a call it already made)") in arch
