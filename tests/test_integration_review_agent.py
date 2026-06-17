from pathlib import Path

WF = Path("src/imp/assets/workflows/imp")


def test_integration_review_agent_declares_verdict_enum() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "integration-review.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert "verdict" in fields
    assert set(fields["verdict"].values) == {"ok", "breakage"}
    assert "status" in fields
    assert set(fields["status"].values) == {"Success", "Error"}
    assert "summary" in fields


def test_integration_review_md_is_non_soliciting() -> None:
    body = (WF / "agents" / "integration-review.md").read_text()
    assert "complete_step" in body
    assert "base-side incoming diff" in body
