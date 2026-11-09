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
