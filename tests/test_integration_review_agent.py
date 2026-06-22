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
