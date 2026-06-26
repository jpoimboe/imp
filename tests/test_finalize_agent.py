from pathlib import Path

WF = Path("src/imp/assets/workflows/imp")


def test_finalize_no_longer_reviews_integration_resolution() -> None:
    body = (WF / "agents" / "finalize.md").read_text()
    assert "integration-notes.md" not in body
    assert "Review the merge resolution" not in body


def test_finalize_still_decides_merge_or_rework() -> None:
    from imp.engine.loader import parse_agent_file
    agent = parse_agent_file((WF / "agents" / "finalize.md").resolve())
    fields = {f.name: f for f in agent.output_schema.fields}
    assert set(fields["decision"].values) == {"merge", "rework", "pause"}


def test_finalize_forbids_premature_merge_claim() -> None:
    body = (WF / "agents" / "finalize.md").read_text()
    assert "enqueued into the merge queue" in body
    assert 'no "Merged"' in body
    assert "is now on master" in body
    assert "at the queue head" in body


def test_finalize_pause_route_drops_human_availability_reasoning() -> None:
    body = (WF / "agents" / "finalize.md").read_text()
    assert "wait_for_user" not in body
    assert "and none is available" not in body
    assert 'decision="pause"' in body
    assert "non-converging verify-triage" in body
