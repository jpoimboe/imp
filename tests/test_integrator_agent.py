from pathlib import Path

WF = Path("src/imp/assets/workflows/imp")


def test_integrator_declares_deviation_envelope() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "integrator.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert "integration" not in fields           # old clean/review enum is gone
    assert fields["deviation"].type == "bool"
    assert set(fields["status"].values) == {"Success", "Error"}
    assert "summary" in fields


def test_integrator_body_is_human_free_and_defines_contexts() -> None:
    body = (WF / "agents" / "integrator.md").resolve().read_text()
    assert "wait_for_user" not in body           # the held region is human-free
    assert "breakage" not in body.lower()
    assert "deviation: true" in body             # escalate criterion
    assert "deviation: false" in body            # reconcile criterion
    assert "conflicted rebase" in body.lower()   # entry context 1
    assert "integration-verify" in body          # entry context 2
    assert "integration-review" in body          # entry context 3
