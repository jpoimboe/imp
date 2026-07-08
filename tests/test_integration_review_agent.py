from pathlib import Path

WF = Path("src/imp/assets/workflows/imp")


def test_integration_review_agent_declares_review_envelope() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "integration-review.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert "verdict" not in fields
    assert set(fields["status"].values) == {"Success", "Error"}
    assert fields["blocking"].type == "bool"
    assert fields["has_minor_findings"].type == "bool"
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
    # The tolerated case returns both flags false and falls through to summarizer.
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
