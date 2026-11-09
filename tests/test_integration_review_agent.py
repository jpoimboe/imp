from pathlib import Path

WF = Path("src/imp/assets/workflows/default")


def test_integration_review_agent_declares_review_envelope() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "integration-review.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert "verdict" not in fields
    assert set(fields["status"].values) == {"Success", "Error"}
    assert fields["critical"].type == "int"
    assert fields["major"].type == "int"
    assert fields["minor"].type == "int"
    assert "findings" in fields


def test_integration_review_md_is_non_soliciting_and_uses_rubric() -> None:
    body = (WF / "agents" / "integration-review.md").read_text()
    assert "complete_step" in body
    assert "wait_for_user" not in body
    assert "base-side incoming diff" in body
    assert '{% include "partials/severity.md" %}' in body
    assert "breakage" not in body.lower()


def test_integration_review_adjudicates_verify_failure_triage() -> None:
    body = (WF / "agents" / "integration-review.md").read_text()
    low = body.lower()
    # A triage entry: resumed by the integration-verify gate to adjudicate a failure.
    assert "integration-verify" in body
    assert "adjudicate" in low and "fix-vs-tolerate" in low
    # Worth fixing -> a blocking finding the reconciler fixes forward (driven by the finding).
    assert "never raw test output" in low
    # Not ours -> a needs-decision toleration recorded in deferred.md, source integration-verify.
    assert "DEFERRED integration-verify:" in body
    assert "needs-decision" in low
    # The tolerated case returns all three counts at zero and falls through to summarizer.
    assert "`critical: 0`, `major: 0`, `minor: 0`, `tolerated: true`" in body
    assert "summarizer" in low


def test_integration_review_declares_tolerated_bool_field() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "integration-review.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert fields["tolerated"].type == "bool"


def test_integration_review_emits_tolerated_signal() -> None:
    body = (WF / "agents" / "integration-review.md").read_text()
    # tolerated:true only in the not-ours toleration branch; false otherwise.
    assert "tolerated: true" in body
    assert "tolerated: false" in body
    # the prompt names the persistent var the accept edge copies the field into.
    assert "slow_verify_tolerated" in body


_DESCRIPTION = (
    "At-head semantic-integration review — reason about whether the integrated branch "
    "breaks against the base-side incoming diff or through the quest's own changes, and "
    "whether each reconciliation stayed faithful to the quest's intent. Non-soliciting; "
    "classifies findings and routes them back to the reconciler."
)
_PINS = (
    _DESCRIPTION,
    "You run at the HEAD of the merge queue, after the quest branch was rebased onto the "
    "current base — or, when the base did not move, only to adjudicate a failing slow run "
    "or review a reconciler fix.",
    "Reason over the failure text against the quest's own diff (`git diff \"$B\" HEAD`) "
    "and the base-side incoming diff (empty when the base did not move). This is the "
    "quest's only slow run, so nothing upstream has tested its full diff:",
    "**Worth fixing** — a break the quest's own changes cause, or a genuine silent break "
    "against the base-side incoming diff → count it at Major or above and name the break "
    "in `findings`.",
)


def test_integration_review_covers_the_unmoved_base_and_the_quests_own_diff() -> None:
    from imp.engine.loader import parse_agent_file
    path = WF / "agents" / "integration-review.md"
    flat = " ".join(path.read_text().split())
    for pin in _PINS:
        assert " ".join(pin.split()) in flat, pin[:40]
    assert parse_agent_file(path.resolve()).description == _DESCRIPTION
    assert "the base moved since this quest forked" not in flat
    assert "the base moved, so reason" not in flat
