from pathlib import Path

WF = Path("src/imp/assets/workflows/imp")


def test_integration_resolve_agent_declares_decision_enum() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "integration-resolve.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert set(fields["decision"].values) == {"merge", "rework", "pause"}
    assert set(fields["status"].values) == {"Success", "Error"}
    assert "comments" in fields


def test_integration_resolve_md_solicits_and_explains_requeue() -> None:
    body = (WF / "agents" / "integration-resolve.md").read_text()
    assert "wait_for_user" in body              # this IS the human gate
    assert "re-enqueue" in body or "back of the queue" in body
