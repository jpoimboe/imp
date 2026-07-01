from pathlib import Path

WF = Path("src/imp/assets/workflows/imp")


def test_reconciler_declares_stuck_envelope() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "reconciler.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert "deviation" not in fields             # the deviation escalation is gone
    assert fields["stuck"].type == "bool"
    assert set(fields["status"].values) == {"Success", "Error"}
    assert "summary" in fields


def test_reconciler_body_is_human_free_and_defines_contexts() -> None:
    body = (WF / "agents" / "reconciler.md").resolve().read_text()
    assert "wait_for_user" not in body           # the held region is human-free
    assert "breakage" not in body.lower()
    assert "stuck: true" in body                 # escalate criterion
    assert "stuck: false" in body                # reconcile criterion
    assert "conflicted rebase" in body.lower()   # entry context 1
    assert "integration-verify" in body          # both in merged context 2
    assert "integration-review" in body          # both in merged context 2
    assert "integration-notes.md" not in body    # no bespoke integration artifact


def test_reconciler_is_finding_driven_not_raw_test_output() -> None:
    body = (WF / "agents" / "reconciler.md").read_text()
    low = body.lower()
    # Collapsed to two entry contexts: an integration-verify failure arrives only as a finding.
    assert "two entry contexts" in low
    assert "three entry contexts" not in low
    assert "never raw test output" in low
