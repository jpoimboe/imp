from pathlib import Path

WF = Path("src/imp/assets/workflows/imp")


def test_integrator_declares_stuck_envelope() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "integrator.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert "deviation" not in fields             # the deviation escalation is gone
    assert fields["stuck"].type == "bool"
    assert set(fields["status"].values) == {"Success", "Error"}
    assert "summary" in fields


def test_integrator_body_is_human_free_and_defines_contexts() -> None:
    body = (WF / "agents" / "integrator.md").resolve().read_text()
    assert "wait_for_user" not in body           # the held region is human-free
    assert "breakage" not in body.lower()
    assert "stuck: true" in body                 # escalate criterion
    assert "stuck: false" in body                # reconcile criterion
    assert "conflicted rebase" in body.lower()   # entry context 1
    assert "integration-verify" in body          # entry context 2
    assert "integration-review" in body          # entry context 3
    assert "integration-notes.md" not in body    # no bespoke integration artifact
